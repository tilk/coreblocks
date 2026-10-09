from collections.abc import Sequence

import pytest
from transactron.lib.adapters import Adapter
from transactron.utils import DependencyContext, ModuleConnector
from coreblocks.interface.keys import CoreStateKey
from coreblocks.interface.layouts import RetirementLayouts
from coreblocks.params import GenParams, configurations
from coreblocks.arch import OpType
from coreblocks.scheduler.scheduler import Scheduler
from transactron.testing import SimpleTestCircuit, TestCaseWithSimulator, TestbenchContext, TestbenchIO, def_method_mock
from .test_scheduler import MockedBlockComponent


@pytest.mark.parametrize("ways, optype_sets", [
    (1, [set(OpType)]),
    (1, [{OpType.ARITHMETIC, OpType.COMPARE}, {OpType.MUL, OpType.COMPARE}, {OpType.DIV_REM, OpType.COMPARE}]),
    (2, [{OpType.ARITHMETIC}, {OpType.ARITHMETIC, OpType.COMPARE}, {OpType.MUL, OpType.DIV_REM}])
])
class TestScheduler2(TestCaseWithSimulator):  # TODO: rename
    def test_scheduler(self, ways: int, optype_sets: Sequence[set[OpType]]):
        gen_params = GenParams(configurations.test.replace(allow_partial_extensions=True, frontend_superscalarity=ways, func_units_config=tuple(MockedBlockComponent(optypes, rs_entries=4) for optypes in optype_sets)))

        sched = SimpleTestCircuit(Scheduler(gen_params=gen_params))

        core_state = TestbenchIO(
            Adapter(o=gen_params.get(RetirementLayouts).core_state, nonexclusive=True)
        )
        dm = DependencyContext.get()
        dm.add_dependency(CoreStateKey(), core_state.adapter.iface)

        m = ModuleConnector(sched=sched, core_state=core_state)

        @def_method_mock(lambda: sched.get_instr)
        def get_instr():
            pass

        print(ways, len(sched._dut.get_free_reg))
        for i in range(ways):
            @def_method_mock(lambda: sched.get_free_reg[i])
            def get_free_reg():
                pass

            # TODO: def_method_mocks
            locals()[f"get_free_reg_{i}"] = get_free_reg
            del get_free_reg

        @def_method_mock(lambda: sched.crat_commit_checkpoint)
        def crat_commit_checkpoint():
            pass

        @def_method_mock(lambda: sched.crat_active_tags)
        def crat_active_tags():
            pass

        @def_method_mock(lambda: sched.rob_put)
        def rob_put():
            pass

        for i in range(ways):
            @def_method_mock(lambda: sched.rf_read_req[i])
            def rf_read_req():
                pass
            
            @def_method_mock(lambda: sched.rf_read_resp[i])
            def rf_read_resp():
                pass

            # TODO: def_method_mocks
            locals()[f"rf_read_req_{i}"] = rf_read_req
            locals()[f"rf_read_resp_{i}"] = rf_read_resp
            del rf_read_req
            del rf_read_resp

        for i, _ in enumerate(optype_sets):
            @def_method_mock(lambda: sched.rs_select[i])
            def rs_select():
                pass

            @def_method_mock(lambda: sched.rs_insert[i])
            def rs_insert():
                pass

            # TODO: find way to avoid this, these are possibly heterogenous
            locals()[f"rs_select_{i}"] = rs_select
            locals()[f"rs_insert_{i}"] = rs_insert
            del rs_select
            del rs_insert

        @def_method_mock(lambda: core_state)
        def core_state_mock() -> dict[str, int]:
            # TODO: flushing test
            return {"flushing": 0}

        async def keep_alive(ctx: TestbenchContext):
            pass

        with self.run_simulation(m) as sim:
            sim.add_testbench(keep_alive)
