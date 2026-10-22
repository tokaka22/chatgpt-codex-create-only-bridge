"""Operator-owned stdio guard. Codex commands stay read-only; one publisher is allowed."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import uuid

MAX_LINE = 64 * 1024 * 1024
READ_METHODS = {
    'initialize', 'account/read', 'account/rateLimits/read', 'model/list',
    'config/read', 'experimentalFeature/list', 'mcpServerStatus/list',
    'thread/read', 'thread/list', 'thread/loaded/list',
    'thread/backgroundTerminals/list', 'thread/unsubscribe',
}

class PolicyError(Exception):
    pass

def require(condition, message):
    if not condition:
        raise PolicyError('CREATE_ONLY_POLICY: ' + message)

def validate_config(config, policy, check_helper=True):
    require(isinstance(config, dict), 'No effective configuration was returned.')
    require(config.get('sandbox_mode') == 'read-only', 'Filesystem must be read-only.')
    require(config.get('approval_policy') == 'never', 'Approval policy must be never.')
    require(config.get('default_permissions') in (None, ':read-only'), 'Unexpected permission profile.')
    require(config.get('notify') in (None, []), 'External notify callbacks must be disabled.')
    require(config.get('web_search') == 'disabled', 'Web search must be disabled for this worker.')
    require(config.get('apps', {}).get('_default', {}).get('enabled') is False, 'Apps must be disabled.')
    for name, app in config.get('apps', {}).items():
        require(isinstance(app, dict) and app.get('enabled') is False, 'Unexpected enabled app: ' + name)
    for feature in policy['required_false_features']:
        require(config.get('features', {}).get(feature) is False, 'Unexpected feature: ' + feature)
    for feature in policy.get('required_true_features',[]):
        require(config.get('features',{}).get(feature) is True, 'Required routing feature missing: '+feature)
    for name, plugin in config.get('plugins', {}).items():
        require(isinstance(plugin, dict) and plugin.get('enabled') is False,
                'Unexpected enabled plugin: ' + name)
    servers = config.get('mcp_servers', {})
    require(isinstance(servers, dict), 'Invalid MCP configuration.')
    expected_name = policy['publisher_server']
    for name, server in servers.items():
        require(isinstance(server, dict), 'Invalid MCP server.')
        if name != expected_name:
            require(server.get('enabled') is False, 'Unexpected enabled MCP server: ' + name)
    actual = servers.get(expected_name)
    expected = policy['publisher']
    require(isinstance(actual, dict), 'Publisher MCP is unavailable.')
    for key in expected:
        require(actual.get(key, False) == expected.get(key) if key == 'required' else actual.get(key) == expected.get(key), 'Publisher configuration mismatch: ' + key)
    for key, value in actual.items():
        if key not in expected:
            require(value is None or value is False or value == [] or value == {}, 'Unexpected publisher field: ' + key)
    require(not actual.get('url') and not actual.get('env') and not actual.get('env_vars'),
            'Publisher transport/environment override is forbidden.')
    if check_helper:
        helper = Path(policy['publisher']['args'][2])
        require(hashlib.sha256(helper.read_bytes()).hexdigest() == policy['publisher_sha256'],
                'Publisher code does not match the reviewed operator version.')

def validate_request(message, known_threads):
    method = message.get('method')
    params = message.get('params') or {}
    require(isinstance(params, dict), 'Invalid native request.')
    if method in ('thread/resume', 'thread/fork'):
        raise PolicyError('CREATE_ONLY_POLICY: Historical execution contexts are sealed. '
                          'Start a fresh agent/context automatically; existing documents remain readable.')
    if method == 'thread/start':
        allowed = {'cwd','sandbox','approvalPolicy','approvalsReviewer','permissions','config','model',
                   'serviceTier','experimentalRawEvents','ephemeral','baseInstructions','developerInstructions',
                   'personality','persistExtendedHistory'}
        require(not set(params) - allowed, 'Unexpected thread parameters: ' + ','.join(sorted(set(params) - allowed)))
        require(params.get('sandbox') == 'read-only' and params.get('approvalPolicy') == 'never',
                'Fresh threads must request read-only/never.')
        require(params.get('permissions') in (None, ':read-only'), 'A writable profile is forbidden.')
        require(isinstance(params.get('cwd'), str) and os.path.isabs(params['cwd']), 'An absolute cwd is required.')
    elif method in ('turn/start', 'turn/steer', 'turn/interrupt'):
        thread_id = params.get('threadId')
        require(thread_id in known_threads, 'Start a fresh agent/context; this native worker does not own that thread.')
        if method in ('turn/steer','turn/interrupt'):
            allowed = {'threadId','input','expectedTurnId'} if method == 'turn/steer' else {'threadId','turnId'}
            require(not set(params) - allowed, 'Unexpected control parameters.')
        if method == 'turn/start':
            allowed = {'threadId','input','cwd','approvalPolicy','approvalsReviewer','permissions',
                       'sandboxPolicy','model','effort','summary','serviceTier','outputSchema','collaborationMode'}
            require(not set(params) - allowed, 'Unexpected turn parameters: ' + ','.join(sorted(set(params) - allowed)))
            require(params.get('approvalPolicy') == 'never', 'Turn approvals must remain never.')
            permission = params.get('permissions')
            sandbox = params.get('sandboxPolicy')
            require(permission == ':read-only' or isinstance(sandbox, dict), 'Turn has no read-only policy.')
            if sandbox is not None:
                require(isinstance(sandbox, dict) and sandbox.get('type') == 'readOnly'
                        and sandbox.get('networkAccess', False) is False and not sandbox.get('writableRoots'),
                        'Turn must retain read-only filesystem and disabled network.')
            require(permission in (None, ':read-only'), 'A writable profile is forbidden.')
            if params.get('cwd') is not None:
                require(os.path.realpath(params['cwd']) == known_threads[thread_id], 'Turn cwd changed.')
    elif method == 'mcpServerStatus/list':
        if params.get('threadId') is not None:
            require(params['threadId'] in known_threads, 'MCP inventory requires a newly guarded thread.')
    else:
        require(method in READ_METHODS or method == 'initialized', 'Native method is not allowed: ' + str(method))
    overrides = params.get('config') or {}
    require(isinstance(overrides, dict), 'Task config must be an object.')
    for key, value in overrides.items():
        require((key == 'apps._default.default_tools_approval_mode' and value in ('auto','approve'))
                or (key == 'model_reasoning_effort' and value in ('none','minimal','low','medium','high','xhigh','max','ultra')),
                'Task configuration cannot override operator security or executable settings.')
    return params

def validate_thread_result(result, cwd):
    require(isinstance(result, dict), 'No native thread result.')
    require(result.get('approvalPolicy') == 'never', 'Native thread did not confirm never approvals.')
    sandbox = result.get('sandbox')
    require(isinstance(sandbox, dict) and sandbox.get('type') == 'readOnly'
            and sandbox.get('networkAccess', False) is False, 'Native thread did not confirm read-only/no-network.')
    require(os.path.realpath(result.get('cwd', '')) == os.path.realpath(cwd), 'Native cwd mismatch.')
    thread_id = result.get('thread', {}).get('id')
    require(isinstance(thread_id, str) and thread_id, 'Native thread has no ID.')
    return thread_id

def validate_tool_call(params, known_threads, active_turns):
    require(isinstance(params, dict), 'Invalid publisher call.')
    require(params.get('threadId') in known_threads and params.get('threadId') in active_turns,
            'Publisher call does not belong to a guarded active turn.')
    require(params.get('namespace') is None and params.get('tool') == 'publish_document',
            'Only the operator publisher function is allowed.')
    require(isinstance(params.get('turnId'), str) and params['turnId'], 'Publisher turn ID missing.')
    expected = active_turns[params['threadId']]
    require(expected == params['turnId'], 'Publisher turn ID mismatch.')
    require(isinstance(params.get('arguments'), dict), 'Publisher arguments must be an object.')
    return params['arguments']


def run_proxy(policy):
    helper = Path(policy['publisher']['args'][2])
    require(hashlib.sha256(helper.read_bytes()).hexdigest() == policy['publisher_sha256'], 'Unreviewed publisher source.')
    spec = importlib.util.spec_from_file_location('operator_create_only_publisher', helper)
    publisher_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = publisher_module
    spec.loader.exec_module(publisher_module)
    publisher = publisher_module.Publisher(policy['root'], policy['source_roots'])
    definition = publisher_module.tool_definition(publisher.limits)
    direct_tool = {'type':'function','name':'publish_document','description':definition['description'],
                   'inputSchema':definition['inputSchema'],'deferLoading':False}
    child = subprocess.Popen([policy['cli'], *policy['cli_args']], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=None, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin.buffer, selectors.EVENT_READ, 'client')
    selector.register(child.stdout, selectors.EVENT_READ, 'native')
    buffers = {'client': bytearray(), 'native': bytearray()}
    checks, starts, known_threads = {}, {}, {}
    turn_starts, active_turns = {}, {}
    pending_turns, terminal_turns = set(), set()
    stop = False
    def stopping(*unused):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, stopping)
    signal.signal(signal.SIGINT, stopping)
    def native(value):
        child.stdin.write(json.dumps(value, ensure_ascii=False).encode() + b'\n')
        child.stdin.flush()
    def emit(value):
        sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode() + b'\n')
        sys.stdout.buffer.flush()
    def reject(request, error):
        emit({'id': request.get('id'), 'error': {'code': -32080, 'message': str(error)}})
    def audit(event, **details):
        print(json.dumps({'createOnlyGuard': event, **details}), file=sys.stderr, flush=True)
    def admit(request, cwd):
        tag = 'create-only-guard-' + uuid.uuid4().hex
        checks[tag] = (request, cwd, time.monotonic())
        native({'id': tag, 'method': 'config/read', 'params': {'cwd': cwd, 'includeLayers': False}})
    def client_message(request):
        if 'method' not in request:
            native(request)  # Responses to native questions; no operation admission.
            return
        params = validate_request(request, known_threads)
        method = request['method']
        if method == 'thread/start':
            admit(request, os.path.realpath(params['cwd']))
        elif method == 'turn/start':
            admit(request, known_threads[params['threadId']])
        elif method == 'mcpServerStatus/list':
            admit(request, known_threads.get(params.get('threadId'), policy['project']))
        else:
            if method == 'turn/interrupt':
                thread_id, turn_id = params.get('threadId'), params.get('turnId')
                if active_turns.get(thread_id) == turn_id:
                    active_turns.pop(thread_id,None)
                    terminal_turns.add((thread_id,turn_id))
                    pending_turns.discard(thread_id)
            native(request)
    def native_message(response):
        identifier = response.get('id')
        if identifier in checks:
            request, cwd, unused = checks.pop(identifier)
            try:
                require('error' not in response, 'Effective configuration could not be verified.')
                validate_config(response.get('result', {}).get('config'), policy)
                if request['method'] == 'thread/start':
                    starts[request['id']] = cwd
                forwarded = request
                if request['method'] == 'thread/start':
                    forwarded = {**request, 'params':{**request['params'], 'dynamicTools':[direct_tool]}}
                elif request['method'] == 'turn/start':
                    thread_id = request['params']['threadId']
                    pending_turns.add(thread_id)
                    turn_starts[request['id']] = thread_id
                native(forwarded)
                audit('admitted', method=request['method'], cwd=cwd)
            except (PolicyError, OSError, KeyError) as error:
                reject(request, error)
                audit('denied', reason=str(error))
            return
        if response.get('method') == 'item/tool/call' and identifier is not None:
            try:
                params = response.get('params')
                arguments = validate_tool_call(params, known_threads, active_turns)
                active_turns[params['threadId']] = params['turnId']
                receipt = publisher.publish(arguments)
                native({'id':identifier,'result':{'contentItems':[{'type':'inputText','text':json.dumps(receipt,ensure_ascii=False)}],
                                                  'success':receipt['status']=='complete'}})
                audit('publication-result',status=receipt['status'],publication=receipt['publication_directory'],
                      files=len(receipt['files']),bytes=receipt['total_bytes_written'])
            except (PolicyError,KeyError,TypeError) as error:
                native({'id':identifier,'result':{'contentItems':[{'type':'inputText','text':str(error)}],'success':False}})
                audit('publisher-denied',reason=str(error))
            return
        if response.get('method') == 'turn/started':
            params = response.get('params',{})
            thread_id, turn_id = params.get('threadId'), params.get('turn',{}).get('id')
            if thread_id in pending_turns and isinstance(turn_id,str) and (thread_id,turn_id) not in terminal_turns:
                require(active_turns.get(thread_id) in (None,turn_id),'Conflicting native turn start.')
                active_turns[thread_id] = turn_id
        if identifier in turn_starts and 'method' not in response:
            thread_id = turn_starts.pop(identifier)
            turn_id = response.get('result',{}).get('turn',{}).get('id')
            if 'error' in response:
                active_turns.pop(thread_id,None)
            elif turn_id and thread_id in pending_turns and (thread_id,turn_id) not in terminal_turns:
                require(active_turns.get(thread_id) in (None,turn_id),'Native active turn ID changed.')
                active_turns[thread_id] = turn_id
            pending_turns.discard(thread_id)
        if response.get('method') == 'turn/completed':
            params = response.get('params',{})
            thread_id, turn_id = params.get('threadId'), params.get('turn',{}).get('id')
            if thread_id in known_threads and isinstance(turn_id,str):
                terminal_turns.add((thread_id,turn_id))
                if active_turns.get(thread_id) == turn_id:
                    active_turns.pop(thread_id,None)
                pending_turns.discard(thread_id)
        if identifier in starts and 'method' not in response:
            cwd = starts.pop(identifier)
            if 'result' in response:
                try:
                    thread_id = validate_thread_result(response['result'], cwd)
                    known_threads[thread_id] = cwd
                    audit('thread-confirmed-readonly', threadId=thread_id)
                except PolicyError as error:
                    reject({'id': identifier}, error)
                    raise  # Stop this worker if native policy differed from the verified request.
        if response.get('method') and identifier is not None and (
                'Approval' in response['method'] or 'elicitation' in response['method']):
            native({'id': identifier, 'error': {'code': -32081,
                    'message': 'Create-only worker does not accept escalation or approval requests.'}})
            audit('escalation-denied', method=response['method'])
            return
        emit(response)
    try:
        audit('started', publisher=policy['publisher_server'])
        while not stop and child.poll() is None:
            for key, unused in selector.select(0.5):
                side = key.data
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    stop = True
                    break
                buffers[side].extend(chunk)
                require(len(buffers[side]) <= MAX_LINE, 'Native protocol line exceeds limit.')
                while b'\n' in buffers[side]:
                    line, _, rest = buffers[side].partition(b'\n')
                    buffers[side] = bytearray(rest)
                    if not line.strip():
                        continue
                    message = json.loads(line)
                    require(isinstance(message, dict), 'Invalid protocol message.')
                    if side == 'client':
                        try:
                            client_message(message)
                        except (PolicyError, KeyError, TypeError, ValueError) as error:
                            reject(message, error)
                    else:
                        native_message(message)
            for identifier, (request, cwd, when) in list(checks.items()):
                if time.monotonic() - when > 25:
                    checks.pop(identifier)
                    reject(request, 'CREATE_ONLY_POLICY: Effective configuration check timed out; no turn was admitted.')
    finally:
        publisher.close()
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()

def main():
    require(len(sys.argv) >= 4 and sys.argv[1] == '--policy', 'Operator policy argument is required.')
    policy = json.loads(Path(sys.argv[2]).read_text())
    args = sys.argv[3:]
    if args[0] != 'app-server' or any(x in args for x in ('generate-json-schema', 'generate-ts')):
        os.execv(policy['cli'], [policy['cli'], *args])
    run_proxy(policy)

if __name__ == '__main__':
    main()
