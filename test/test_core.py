from amaranth import Elaboratable, Module
from amaranth.lib.wiring import connect

from transactron.lib import AdapterTrans
from transactron.utils import align_to_power_of_two, signed_to_int

from transactron.testing import TestCaseWithSimulator, TestbenchIO

from coreblocks.core import Core
from coreblocks.frontend.decoder import Opcode, Funct3
from coreblocks.params import GenParams
from coreblocks.params.instr import *
from coreblocks.params.configurations import CoreConfiguration, basic_core_config, full_core_config
from coreblocks.peripherals.wishbone import WishboneSignature, WishboneMemorySlave

from typing import Optional
import random
import subprocess
import tempfile
from parameterized import parameterized_class


class CoreTestElaboratable(Elaboratable):
    def __init__(self, gen_params: GenParams, instr_mem: list[int] = [0], data_mem: Optional[list[int]] = None):
        self.gen_params = gen_params
        self.instr_mem = instr_mem
        if data_mem is None:
            self.data_mem = [0] * (2**10)
        else:
            self.data_mem = data_mem

    def elaborate(self, platform):
        m = Module()

        wb_instr_bus = WishboneSignature(self.gen_params.wb_params).create()
        wb_data_bus = WishboneSignature(self.gen_params.wb_params).create()

        # Align the size of the memory to the length of a cache line.
        instr_mem_depth = align_to_power_of_two(len(self.instr_mem), self.gen_params.icache_params.line_bytes_log)
        self.wb_mem_slave = WishboneMemorySlave(
            wb_params=self.gen_params.wb_params, width=32, depth=instr_mem_depth, init=self.instr_mem
        )
        self.wb_mem_slave_data = WishboneMemorySlave(
            wb_params=self.gen_params.wb_params, width=32, depth=len(self.data_mem), init=self.data_mem
        )
        self.core = Core(gen_params=self.gen_params, wb_instr_bus=wb_instr_bus, wb_data_bus=wb_data_bus)
        self.io_in = TestbenchIO(AdapterTrans(self.core.fetch_continue.method))
        self.interrupt = TestbenchIO(AdapterTrans(self.core.interrupt_controller.report_interrupt))

        m.submodules.wb_mem_slave = self.wb_mem_slave
        m.submodules.wb_mem_slave_data = self.wb_mem_slave_data
        m.submodules.c = self.core
        m.submodules.io_in = self.io_in
        m.submodules.interrupt = self.interrupt

        connect(m, wb_instr_bus, self.wb_mem_slave.bus)
        connect(m, wb_data_bus, self.wb_mem_slave_data.bus)

        return m


class TestCoreBase(TestCaseWithSimulator):
    gen_params: GenParams
    m: CoreTestElaboratable

    def get_phys_reg_rrat(self, reg_id):
        return (yield self.m.core.RRAT.entries[reg_id])

    def get_arch_reg_val(self, reg_id):
        return (yield self.m.core.RF.entries[(yield from self.get_phys_reg_rrat(reg_id))].reg_val)

    def push_instr(self, opcode):
        yield from self.m.io_in.call(instr=opcode)

    def push_register_load_imm(self, reg_id, val):
        addi_imm = signed_to_int(val & 0xFFF, 12)
        lui_imm = (val & 0xFFFFF000) >> 12
        # handle addi sign extension, see: https://stackoverflow.com/a/59546567
        if val & 0x800:
            lui_imm = (lui_imm + 1) & (0xFFFFF)

        yield from self.push_instr(UTypeInstr(opcode=Opcode.LUI, rd=reg_id, imm=lui_imm << 12).encode())
        yield from self.push_instr(
            ITypeInstr(opcode=Opcode.OP_IMM, rd=reg_id, funct3=Funct3.ADD, rs1=reg_id, imm=addi_imm).encode()
        )


class TestCoreAsmSourceBase(TestCoreBase):
    base_dir: str = "test/asm/"

    def prepare_source(self, filename):
        bin_src = []
        with (
            tempfile.NamedTemporaryFile() as asm_tmp,
            tempfile.NamedTemporaryFile() as ld_tmp,
            tempfile.NamedTemporaryFile() as bin_tmp,
        ):
            subprocess.check_call(
                [
                    "riscv64-unknown-elf-as",
                    "-mabi=ilp32",
                    # Specified manually, because toolchains from most distributions don't support new extensioins
                    # and this test should be accessible locally.
                    "-march=rv32im_zicsr",
                    "-o",
                    asm_tmp.name,
                    self.base_dir + filename,
                ]
            )
            subprocess.check_call(
                [
                    "riscv64-unknown-elf-ld",
                    "-m",
                    "elf32lriscv",
                    "-T",
                    self.base_dir + "link.ld",
                    asm_tmp.name,
                    "-o",
                    ld_tmp.name,
                ]
            )
            subprocess.check_call(
                ["riscv64-unknown-elf-objcopy", "-O", "binary", "-j", ".text", ld_tmp.name, bin_tmp.name]
            )
            code = bin_tmp.read()
            for word_idx in range(0, len(code), 4):
                word = code[word_idx : word_idx + 4]
                bin_instr = int.from_bytes(word, "little")
                bin_src.append(bin_instr)

        return bin_src


@parameterized_class(
    ("name", "source_file", "cycle_count", "expected_regvals", "configuration"),
    [
        ("fibonacci", "fibonacci.asm", 500, {2: 2971215073}, basic_core_config),
        ("fibonacci_mem", "fibonacci_mem.asm", 400, {3: 55}, basic_core_config),
        ("csr", "csr.asm", 200, {1: 1, 2: 4}, full_core_config),
        ("exception", "exception.asm", 200, {1: 1, 2: 2}, basic_core_config),
        ("exception_mem", "exception_mem.asm", 200, {1: 1, 2: 2}, basic_core_config),
        ("exception_handler", "exception_handler.asm", 2000, {2: 987, 11: 0xAAAA, 15: 16}, full_core_config),
    ],
)
class TestCoreBasicAsm(TestCoreAsmSourceBase):
    source_file: str
    cycle_count: int
    expected_regvals: dict[int, int]
    configuration: CoreConfiguration

    def run_and_check(self):
        for _ in range(self.cycle_count):
            yield

        for reg_id, val in self.expected_regvals.items():
            assert (yield from self.get_arch_reg_val(reg_id)) == val

    def test_asm_source(self):
        self.gen_params = GenParams(self.configuration)

        bin_src = self.prepare_source(self.source_file)
        self.m = CoreTestElaboratable(self.gen_params, instr_mem=bin_src)
        with self.run_simulation(self.m) as sim:
            sim.add_sync_process(self.run_and_check)


# test interrupts with varying triggering frequency (parametrizable amount of cycles between
# returning from an interrupt and triggering it again with 'lo' and 'hi' parameters)
@parameterized_class(
    ("source_file", "main_cycle_count", "start_regvals", "expected_regvals", "lo", "hi"),
    [
        ("interrupt.asm", 400, {4: 2971215073, 8: 29}, {2: 2971215073, 7: 29, 31: 0xDE}, 300, 500),
        ("interrupt.asm", 700, {4: 24157817, 8: 199}, {2: 24157817, 7: 199, 31: 0xDE}, 100, 200),
        ("interrupt.asm", 600, {4: 89, 8: 843}, {2: 89, 7: 843, 31: 0xDE}, 30, 50),
        # interrupts are only inserted on branches, we always have some forward progression. 15 for trigger variantion.
        ("interrupt.asm", 80, {4: 21, 8: 9349}, {2: 21, 7: 9349, 31: 0xDE}, 0, 15),
    ],
)
class TestCoreInterrupt(TestCoreAsmSourceBase):
    source_file: str
    main_cycle_count: int
    start_regvals: dict[int, int]
    expected_regvals: dict[int, int]
    lo: int
    hi: int

    def setup_method(self):
        self.configuration = full_core_config
        self.gen_params = GenParams(self.configuration)
        random.seed(1500100900)

    def run_with_interrupt(self):
        main_cycles = 0
        int_count = 0

        # set up fibonacci max numbers
        for reg_id, val in self.start_regvals.items():
            yield from self.push_register_load_imm(reg_id, val)
        # wait for caches to fill up so that mtvec is written - very important
        # TODO: replace with interrupt enable via CSR
        yield from self.tick(200)

        early_interrupt = False
        while main_cycles < self.main_cycle_count or early_interrupt:
            if not early_interrupt:
                # run main code for some semi-random amount of cycles
                c = random.randrange(self.lo, self.hi)
                main_cycles += c
                yield from self.tick(c)
                # trigger an interrupt
                yield from self.m.interrupt.call()
                yield
                int_count += 1

            # wait for the interrupt to get registered
            while (yield self.m.core.interrupt_controller.interrupts_enabled) == 1:
                yield

            # trigger interrupt during execution of ISR handler (blocked-pending) with some chance
            early_interrupt = random.random() < 0.4
            if early_interrupt:
                yield from self.m.interrupt.call()
                yield
                int_count += 1

            # wait until ISR returns
            while (yield self.m.core.interrupt_controller.interrupts_enabled) == 0:
                yield

        assert (yield from self.get_arch_reg_val(30)) == int_count
        for reg_id, val in self.expected_regvals.items():
            assert (yield from self.get_arch_reg_val(reg_id)) == val

    def test_interrupted_prog(self):
        bin_src = self.prepare_source(self.source_file)
        self.m = CoreTestElaboratable(self.gen_params, instr_mem=bin_src)
        with self.run_simulation(self.m) as sim:
            sim.add_sync_process(self.run_with_interrupt)
