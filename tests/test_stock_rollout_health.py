"""Offline regression for preserving the Docker health probe through two rollouts."""
import ast
from copy import deepcopy
from pathlib import Path
import unittest


class StockRolloutHealthTests(unittest.TestCase):
    def setUp(self):
        # The host module requires Linux fcntl/root; compile its actual pure command builder.
        source=Path(__file__).resolve().parents[1]/'scripts/oracle_unified_rollout_host.py'
        tree=ast.parse(source.read_text(encoding='utf-8'))
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='create_replacement')
        self.commands=[]
        namespace={'run':lambda args:self.commands.append(args) or 'fixture-container'}
        exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),namespace)
        self.create=namespace['create_replacement']
        self.old={'Id':'fixture-id','Config':{'User':'1001:1001','WorkingDir':'/app','Env':[],
                  'Entrypoint':['python'],'Cmd':['-m','stock_assistant'],
                  'Healthcheck':{'Test':['CMD','python','-c',"assert {'orders_enabled':False}['orders_enabled'] is False"],
                                 'Interval':30_000_000_000,'Timeout':5_000_000_000,'StartPeriod':10_000_000_000,'Retries':3}},
                  'HostConfig':{'ReadonlyRootfs':True,'RestartPolicy':{'Name':'unless-stopped'}},'Mounts':[],
                  'NetworkSettings':{'Networks':{'fixture-private':{'Aliases':[]}}}}

    def test_exec_probe_survives_the_shell_form_returned_by_docker(self):
        self.create(self.old,'fixture-name','fixture-image')
        first=self.commands[-1]
        second=deepcopy(self.old)
        second['Config']['Healthcheck']['Test']=['CMD-SHELL',first[first.index('--health-cmd')+1]]
        self.create(second,'fixture-name','fixture-image')
        self.assertEqual(self.commands[-1],first)
        for flag,value in [('--health-interval','30000000000ns'),('--health-timeout','5000000000ns'),
                           ('--health-start-period','10000000000ns'),('--health-retries','3')]:
            self.assertEqual(first[first.index(flag)+1],value)

    def test_explicitly_disabled_probe_is_preserved(self):
        self.old['Config']['Healthcheck']={'Test':['NONE']}
        self.create(self.old,'fixture-name','fixture-image')
        self.assertIn('--no-healthcheck',self.commands[-1])

    def test_unknown_probe_is_rejected_before_docker_mutation(self):
        self.old['Config']['Healthcheck']={'Test':['INVALID']}
        with self.assertRaisesRegex(ValueError,'HEALTHCHECK_CONTRACT'):
            self.create(self.old,'fixture-name','fixture-image')
        self.assertEqual(self.commands,[])
