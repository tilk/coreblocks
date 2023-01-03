import random
from collections import namedtuple, deque
from typing import Callable, Optional, Iterable
from amaranth import *
from amaranth.sim import Settle
from coreblocks.transactions import TransactionModule
from coreblocks.transactions.lib import FIFO, AdapterTrans, Adapter
from coreblocks.scheduler.scheduler import Scheduler
from coreblocks.structs_common.rf import RegisterFile
from coreblocks.structs_common.rat import FRAT
from coreblocks.params import RSLayouts, DecodeLayouts, GenParams, Opcode, OpType, Funct3, Funct7
from coreblocks.structs_common.rob import ReorderBuffer
from coreblocks.utils import AutoDebugSignals
from ..common import RecordIntDict, TestCaseWithSimulator, TestGen, TestbenchIO


class SchedulerTestCircuit(Elaboratable, AutoDebugSignals):
    def __init__(self, gen_params: GenParams):
        self.gen_params = gen_params

    def elaborate(self, platform):
        m = Module()
        tm = TransactionModule(m)

        rs_layouts = self.gen_params.get(RSLayouts)
        decode_layouts = self.gen_params.get(DecodeLayouts)

        with tm.transaction_context():
            # data structures
            m.submodules.instr_fifo = instr_fifo = FIFO(decode_layouts.decoded_instr, 16)
            m.submodules.free_rf_fifo = free_rf_fifo = FIFO(
                self.gen_params.phys_regs_bits, 2**self.gen_params.phys_regs_bits
            )
            m.submodules.rat = rat = FRAT(gen_params=self.gen_params)
            m.submodules.rob = self.rob = ReorderBuffer(self.gen_params)
            m.submodules.rf = self.rf = RegisterFile(gen_params=self.gen_params)

            # mocked RS
            method_rs_alloc = Adapter(o=rs_layouts.select_out)
            method_rs_insert = Adapter(i=rs_layouts.insert_in)

            # mocked input and output
            m.submodules.output = self.out = TestbenchIO(method_rs_insert)
            m.submodules.rs_allocate = self.rs_allocate = TestbenchIO(method_rs_alloc)
            m.submodules.rf_write = self.rf_write = TestbenchIO(AdapterTrans(self.rf.write))
            m.submodules.rf_free = self.rf_free = TestbenchIO(AdapterTrans(self.rf.free))
            m.submodules.rob_markdone = self.rob_done = TestbenchIO(AdapterTrans(self.rob.mark_done))
            m.submodules.rob_retire = self.rob_retire = TestbenchIO(AdapterTrans(self.rob.retire))
            m.submodules.instr_input = self.instr_inp = TestbenchIO(AdapterTrans(instr_fifo.write))
            m.submodules.free_rf_inp = self.free_rf_inp = TestbenchIO(AdapterTrans(free_rf_fifo.write))

            # main scheduler
            m.submodules.scheduler = self.scheduler = Scheduler(
                get_instr=instr_fifo.read,
                get_free_reg=free_rf_fifo.read,
                rat_rename=rat.rename,
                rob_put=self.rob.put,
                rf_read1=self.rf.read1,
                rf_read2=self.rf.read2,
                rs_alloc=method_rs_alloc.iface,
                rs_insert=method_rs_insert.iface,
                gen_params=self.gen_params,
            )

        return tm


class TestScheduler(TestCaseWithSimulator):
    def setUp(self):
        self.gen_params = GenParams("rv32i")
        self.expected_rename_queue = deque()
        self.expected_phys_reg_queue = deque()
        self.free_regs_queue = deque()
        self.free_ROB_entries_queue = deque()
        self.expected_rs_entry_queue = deque()
        self.current_RAT = [0 for _ in range(0, self.gen_params.isa.reg_cnt)]
        self.instr_count = 500
        self.m = SchedulerTestCircuit(self.gen_params)

        random.seed(42)

        # set up static RF state lookup table
        RFEntry = namedtuple("RFEntry", ["value", "valid"])
        self.rf_state = [
            RFEntry(random.randint(0, self.gen_params.isa.xlen - 1), random.randint(0, 1))
            for _ in range(2**self.gen_params.phys_regs_bits)
        ]
        self.rf_state[0] = RFEntry(0, 1)

        for i in range(1, 2**self.gen_params.phys_regs_bits):
            self.free_phys_reg(i)

    def free_phys_reg(self, reg_id):
        self.free_regs_queue.append({"data": reg_id})
        self.expected_phys_reg_queue.append(reg_id)

    def queue_gather(self, queues: Iterable[deque]):
        # Iterate over all 'queues' and take one element from each, gathering
        # all key-value pairs into 'item'.
        item = {}
        for q in queues:
            partial_item = None
            # retry until we get an element
            while partial_item is None:
                # get element from one queue
                if q:
                    partial_item = q.popleft()
                    # None signals to end the process
                    if partial_item is None:
                        return None
                else:
                    # if no element available, wait and retry on the next clock cycle
                    yield

            # merge queue element with all previous ones (dict merge)
            item = item | partial_item
        return item

    def make_queue_process(
        self,
        *,
        io: TestbenchIO,
        input_queues: Optional[Iterable[deque]] = None,
        output_queues: Optional[Iterable[deque]] = None,
        check: Optional[Callable[[RecordIntDict, RecordIntDict], TestGen[None]]] = None,
    ):
        """Create queue gather-and-test process

        This function returns a simulation process that does the following steps:
        1. Gathers dicts from multiple ``queues`` (one dict from each) and joins
           them together (items from queues are popped using popleft)
        2. ``io`` is called with items gathered from ``input_queues``
        3. If ``check`` was supplied, it's called with the results returned from
           call in step 2. and items gathered from ``output_queues``
        Steps 1-3 are repeated until one of the queues receives None

        Intention is to simplify writing tests with queues: ``input_queues`` lets
        the user specify multiple data sources (queues) from which to gather
        arguments for call to ``io``, and multiple data sources (queues) from which
        to gather reference values to test against the results from the call to ``io``.

        Parameters
        ----------
        io : TestbenchIO
            TestbenchIO to call with items gathered from ``input_queues``.
        input_queues : deque[dict], optional
            Queue of dictionaries containing fields and values of a record to call
            ``io`` with. Different fields may be split across multiple queues.
            Fields with the same name in different queues must not be used.
            Dictionaries are popped from the deques using popleft.
        output_queues : deque[dict], optional
            Queue of dictionaries containing reference fields and values to compare
            results of ``io`` call with. Different fields may be split across
            multiple queues. Fields with the same name in different queues must
            not be used. Dictionaries are popped from the deques using popleft.
        check : Callable[[dict, dict], TestGen]
            Testbench generator which will be called with parameters ``result``
            and ``outputs``, meaning results from the call to ``io`` and item
            gathered from ``output_queues``.

        Returns
        -------
        Callable[None, TestGen]
            Simulation process performing steps described above.

        Raises
        ------
        ValueError
            If neither ``input_queues`` nor ``output_queues`` are supplied.
        """

        def queue_process():
            while True:
                inputs = {}
                outputs = {}
                # gather items from both queues
                if input_queues is not None:
                    inputs = yield from self.queue_gather(input_queues)
                if output_queues is not None:
                    outputs = yield from self.queue_gather(output_queues)

                # Check if queues signalled to end the process
                if inputs is None or outputs is None:
                    return

                result = yield from io.call(inputs)

                # this could possibly be extended to automatically compare 'results' and
                # 'outputs' if check is None but that needs some dict deepcompare
                if check is not None:
                    yield Settle()
                    yield from check(result, outputs)

        if output_queues is None and input_queues is None:
            raise ValueError("Either output_queues or input_queues must be supplied")

        return queue_process

    def make_output_process(self):
        def check(got, expected):
            rl_dst = yield self.m.rob.data[got["rs_data"]["rob_id"]].rob_data.rl_dst
            s1 = self.rf_state[expected["rp_s1"]]
            s2 = self.rf_state[expected["rp_s2"]]

            # if source operand register ids are 0 then we already have values
            self.assertEqual(got["rs_data"]["rp_s1"], expected["rp_s1"] if not s1.valid else 0)
            self.assertEqual(got["rs_data"]["rp_s2"], expected["rp_s2"] if not s2.valid else 0)
            self.assertEqual(got["rs_data"]["rp_dst"], expected["rp_dst"])
            self.assertEqual(got["rs_data"]["exec_fn"], expected["exec_fn"])
            self.assertEqual(got["rs_entry_id"], expected["rs_entry_id"])
            self.assertEqual(got["rs_data"]["s1_val"], s1.value if s1.valid else 0)
            self.assertEqual(got["rs_data"]["s2_val"], s2.value if s2.valid else 0)
            self.assertEqual(rl_dst, expected["rl_dst"])

            # recycle physical register number
            if got["rs_data"]["rp_dst"] != 0:
                self.free_phys_reg(got["rs_data"]["rp_dst"])
            # recycle ROB entry
            self.free_ROB_entries_queue.append({"rob_id": got["rs_data"]["rob_id"]})

        return self.make_queue_process(
            io=self.m.out, output_queues=[self.expected_rename_queue, self.expected_rs_entry_queue], check=check
        )

    def test_randomized(self):
        def instr_input_process():
            yield from self.m.rob_retire.enable()

            # set up RF to reflect our static rf_state reference lookup table
            for i in range(2**self.gen_params.phys_regs_bits - 1):
                yield from self.m.rf_write.call({"reg_id": i, "reg_val": self.rf_state[i].value})
                if not self.rf_state[i].valid:
                    yield from self.m.rf_free.call({"reg_id": i})

            for i in range(self.instr_count):
                rl_s1 = random.randint(0, self.gen_params.isa.reg_cnt - 1)
                rl_s2 = random.randint(0, self.gen_params.isa.reg_cnt - 1)
                rl_dst = random.randint(0, self.gen_params.isa.reg_cnt - 1)

                opcode = random.choice(list(Opcode)).value
                op_type = random.choice(list(OpType)).value
                funct3 = random.choice(list(Funct3)).value
                funct7 = random.choice(list(Funct7)).value
                immediate = random.randint(0, 2**32 - 1)
                rp_s1 = self.current_RAT[rl_s1]
                rp_s2 = self.current_RAT[rl_s2]
                rp_dst = self.expected_phys_reg_queue.popleft() if rl_dst != 0 else 0

                self.expected_rename_queue.append(
                    {
                        "rp_s1": rp_s1,
                        "rp_s2": rp_s2,
                        "rl_dst": rl_dst,
                        "rp_dst": rp_dst,
                        "opcode": opcode,
                        "exec_fn": {
                            "op_type": op_type,
                            "funct3": funct3,
                            "funct7": funct7,
                        },
                    }
                )
                self.current_RAT[rl_dst] = rp_dst

                yield from self.m.instr_inp.call(
                    {
                        "opcode": opcode,
                        "illegal": 0,
                        "exec_fn": {
                            "op_type": op_type,
                            "funct3": funct3,
                            "funct7": funct7,
                        },
                        "regs_l": {
                            "rl_s1": rl_s1,
                            "rl_s1_v": 1,
                            "rl_s2": rl_s2,
                            "rl_s2_v": 1,
                            "rl_dst": rl_dst,
                            "rl_dst_v": 1,
                        },
                        "imm": immediate,
                    }
                )
            # Terminate other processes
            self.expected_rename_queue.append(None)
            self.free_regs_queue.append(None)
            self.free_ROB_entries_queue.append(None)

        def rs_alloc_process():
            def mock(_):
                random_entry = random.randint(0, self.gen_params.rs_entries - 1)
                self.expected_rs_entry_queue.append({"rs_entry_id": random_entry})
                return {"rs_entry_id": random_entry}

            def true_n_times(n: int) -> Callable[[], bool]:
                return ([False] + [True] * n).pop

            yield from self.m.rs_allocate.method_handle_loop(mock, settle=1, condition=true_n_times(self.instr_count))
            self.expected_rs_entry_queue.append(None)

        with self.run_simulation(self.m, max_cycles=1500) as sim:
            sim.add_sync_process(self.make_output_process())
            sim.add_sync_process(
                self.make_queue_process(io=self.m.rob_done, input_queues=[self.free_ROB_entries_queue])
            )
            sim.add_sync_process(self.make_queue_process(io=self.m.free_rf_inp, input_queues=[self.free_regs_queue]))
            sim.add_sync_process(instr_input_process)
            sim.add_sync_process(rs_alloc_process)
