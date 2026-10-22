import copy
import hashlib
import importlib.util
from pathlib import Path
import unittest

location=Path(__file__).with_name('runtime_guard.py')
spec=importlib.util.spec_from_file_location('runtime_guard', location)
guard=importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.policy={'publisher_server':'create_only_documents','required_false_features':['plugins','js_repl','apps'],
          'publisher':{'command':'python3','args':['-I','-B',str(location)],'cwd':'/safe/output','enabled':True,
            'required':True,'enabled_tools':['publish_document'],'default_tools_approval_mode':'approve',
            'tools':{'publish_document':{'approval_mode':'approve'}}},
          'publisher_sha256':hashlib.sha256(location.read_bytes()).hexdigest()}
        self.config={'sandbox_mode':'read-only','approval_policy':'never','default_permissions':None,
          'web_search':'disabled','apps':{'_default':{'enabled':False}},
          'features':{'plugins':False,'js_repl':False,'apps':False},
          'plugins':{'old-plugin':{'enabled':False}},
          'mcp_servers':{'node_repl':{'enabled':False},'create_only_documents':copy.deepcopy(self.policy['publisher'])}}
    def bad(self, config):
        with self.assertRaises(guard.PolicyError):guard.validate_config(config,self.policy)
    def test_valid_policy(self):guard.validate_config(self.config,self.policy)
    def test_unknown_mcp_is_denied(self):
        self.config['mcp_servers']['unexpected']={'command':'arbitrary'};self.bad(self.config)
    def test_enabled_old_mcp_is_denied(self):
        self.config['mcp_servers']['node_repl']['enabled']=True;self.bad(self.config)
    def test_enabled_plugin_is_denied(self):
        self.config['plugins']['old-plugin']['enabled']=True;self.bad(self.config)
    def test_repl_feature_is_denied(self):
        self.config['features']['js_repl']=True;self.bad(self.config)
    def test_missing_feature_is_denied(self):
        del self.config['features']['plugins'];self.bad(self.config)
    def test_write_profile_is_denied(self):
        self.config['default_permissions']='old-write-profile';self.bad(self.config)
    def test_publisher_command_override_denied(self):
        self.config['mcp_servers']['create_only_documents']['command']='sh';self.bad(self.config)
    def test_publisher_argument_override_denied(self):
        self.config['mcp_servers']['create_only_documents']['args'].append('other-root');self.bad(self.config)
    def test_publisher_environment_override_denied(self):
        self.config['mcp_servers']['create_only_documents']['env']={'PYTHONPATH':'elsewhere'};self.bad(self.config)
    def test_unreviewed_helper_is_denied(self):
        self.policy['publisher_sha256']='0'*64;self.bad(self.config)
    def test_fresh_thread(self):
        guard.validate_request({'method':'thread/start','params':{'cwd':'/safe','sandbox':'read-only','approvalPolicy':'never'}},{})
    def test_old_resume_and_fork_denied(self):
        for method in ['thread/resume','thread/fork']:
            with self.assertRaises(guard.PolicyError):guard.validate_request({'method':method,'params':{'threadId':'old'}},{})
    def test_unowned_turn_denied(self):
        with self.assertRaises(guard.PolicyError):guard.validate_request({'method':'turn/start','params':{'threadId':'old'}},{})
    def test_owned_readonly_turn(self):
        guard.validate_request({'method':'turn/start','params':{'threadId':'new','approvalPolicy':'never',
          'sandboxPolicy':{'type':'readOnly','networkAccess':False}}},{'new':'/safe'})
    def test_owned_writable_turn_denied(self):
        with self.assertRaises(guard.PolicyError):guard.validate_request({'method':'turn/start','params':{'threadId':'new',
          'approvalPolicy':'never','sandboxPolicy':{'type':'workspaceWrite'}}},{'new':'/safe'})
    def test_configuration_mutation_denied(self):
        with self.assertRaises(guard.PolicyError):guard.validate_request({'method':'config/value/write','params':{}},{})
    def test_security_config_override_denied(self):
        with self.assertRaises(guard.PolicyError):guard.validate_request({'method':'thread/start','params':{'cwd':'/safe',
          'sandbox':'read-only','approvalPolicy':'never','config':{'mcp_servers.evil.enabled':True}}},{})
    def test_notify_callback_override_denied(self):
        with self.assertRaises(guard.PolicyError):guard.validate_request({'method':'thread/start','params':{'cwd':'/safe',
          'sandbox':'read-only','approvalPolicy':'never','config':{'notify':['arbitrary-command']}}},{})
    def test_dynamic_tools_denied(self):
        with self.assertRaises(guard.PolicyError):guard.validate_request({'method':'thread/start','params':{'cwd':'/safe',
          'sandbox':'read-only','approvalPolicy':'never','dynamicTools':[{'name':'bad'}]}},{})
    def test_conflicting_sandbox_and_profile_denied(self):
        with self.assertRaises(guard.PolicyError):guard.validate_request({'method':'turn/start','params':{'threadId':'new',
          'approvalPolicy':'never','permissions':':read-only','sandboxPolicy':{'type':'dangerFullAccess','networkAccess':True}}},{'new':'/safe'})
    def test_inherited_notify_denied(self):
        self.config['notify']=['external-command'];self.bad(self.config)
    def test_per_app_override_denied(self):
        self.config['apps']['other-app']={'enabled':True};self.bad(self.config)
    def test_extra_publisher_transport_field_denied(self):
        self.config['mcp_servers']['create_only_documents']['experimental_environment']={'DYLD_INSERT_LIBRARIES':'bad'};self.bad(self.config)
    def test_old_history_mutation_denied(self):
        with self.assertRaises(guard.PolicyError):guard.validate_request({'method':'thread/archive','params':{'threadId':'old'}},{})
    def test_unowned_mcp_inventory_denied(self):
        with self.assertRaises(guard.PolicyError):guard.validate_request({'method':'mcpServerStatus/list','params':{'threadId':'old'}},{})
    def test_steering_override_denied(self):
        with self.assertRaises(guard.PolicyError):guard.validate_request({'method':'turn/steer','params':{'threadId':'new','input':[], 'dynamicTools':[]}},{'new':'/safe'})
    def test_publisher_environment_must_match_operator(self):
        self.policy['publisher']['environment_id']='local'
        self.config['mcp_servers']['create_only_documents']['environment_id']='remote'
        self.bad(self.config)
    def test_bridge_reasoning_setting_is_bounded(self):
        guard.validate_request({'method':'thread/start','params':{'cwd':'/safe','sandbox':'read-only','approvalPolicy':'never',
          'config':{'model_reasoning_effort':'xhigh','apps._default.default_tools_approval_mode':'auto'}}},{})
        with self.assertRaises(guard.PolicyError):guard.validate_request({'method':'thread/start','params':{'cwd':'/safe','sandbox':'read-only','approvalPolicy':'never',
          'config':{'model_reasoning_effort':{'notify':['command']}}}},{})
    def test_direct_publisher_call_requires_active_owned_turn(self):
        params={'threadId':'new','turnId':'turn1','tool':'publish_document','arguments':{'slug':'test','files':[]}}
        guard.validate_tool_call(params,{'new':'/safe'},{'new':'turn1'})
        with self.assertRaises(guard.PolicyError):guard.validate_tool_call(params,{'new':'/safe'}, {})
        with self.assertRaises(guard.PolicyError):guard.validate_tool_call(params,{'new':'/safe'}, {'new':'other-turn'})
    def test_direct_publisher_rejects_namespaces_and_other_tools(self):
        params={'threadId':'new','turnId':'turn1','tool':'publish_document','namespace':'evil','arguments':{}}
        with self.assertRaises(guard.PolicyError):guard.validate_tool_call(params,{'new':'/safe'},{'new':'turn1'})
        params['namespace']=None;params['tool']='overwrite_file'
        with self.assertRaises(guard.PolicyError):guard.validate_tool_call(params,{'new':'/safe'},{'new':'turn1'})
    def test_unconfirmed_pending_turn_cannot_publish(self):
        params={'threadId':'new','turnId':'turn1','tool':'publish_document','arguments':{}}
        with self.assertRaises(guard.PolicyError):guard.validate_tool_call(params,{'new':'/safe'},{'new':None})
    def test_required_routing_host_is_verified(self):
        self.policy['required_true_features']=['code_mode_host']
        self.config['features']['code_mode_host']=True
        guard.validate_config(self.config,self.policy)
    def test_disabled_or_missing_routing_host_is_rejected(self):
        self.policy['required_true_features']=['code_mode_host']
        self.bad(self.config)
        self.config['features']['code_mode_host']=False
        self.bad(self.config)
    def test_readonly_native_confirmation(self):
        self.assertEqual(guard.validate_thread_result({'cwd':'/safe','approvalPolicy':'never',
          'sandbox':{'type':'readOnly','networkAccess':False},'thread':{'id':'new'}},'/safe'),'new')
    def test_writable_native_confirmation_denied(self):
        with self.assertRaises(guard.PolicyError):guard.validate_thread_result({'cwd':'/safe','approvalPolicy':'never',
          'sandbox':{'type':'workspaceWrite'},'thread':{'id':'new'}},'/safe')

if __name__=='__main__':unittest.main(verbosity=2)
