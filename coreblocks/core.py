from amaranth import *
from amaranth.lib.wiring import flipped, connect

from transactron.utils.dependencies import DependencyManager, DependencyContext
from coreblocks.func_blocks.interface.func_blocks_unifier import FuncBlocksUnifier
from coreblocks.priv.traps.instr_counter import CoreInstructionCounter
from coreblocks.priv.traps.interrupt_controller import InterruptController
from transactron.core import Transaction, TModule
from transactron.lib import FIFO, ConnectTrans
from coreblocks.interface.layouts import *
from coreblocks.interface.keys import (
    BranchVerifyKey,
    FetchResumeKey,
    GenericCSRRegistersKey,
    CommonBusDataKey,
    FlushICacheKey,
)
from coreblocks.params.genparams import GenParams
from coreblocks.frontend.decoder.decode_stage import DecodeStage
from coreblocks.core_structs.rat import FRAT, RRAT
from coreblocks.core_structs.rob import ReorderBuffer
from coreblocks.core_structs.rf import RegisterFile
from coreblocks.priv.csr.csr_instances import GenericCSRRegisters
from coreblocks.priv.traps.exception import ExceptionCauseRegister
from coreblocks.scheduler.scheduler import Scheduler
from coreblocks.backend.annoucement import ResultAnnouncement
from coreblocks.backend.retirement import Retirement
from coreblocks.cache.icache import ICache, ICacheBypass
from coreblocks.peripherals.bus_adapter import WishboneMasterAdapter
from coreblocks.peripherals.wishbone import WishboneMaster, WishboneInterface
from coreblocks.cache.refiller import SimpleCommonBusCacheRefiller
from coreblocks.frontend.fetch.fetch import FetchUnit
from transactron.lib.transformers import MethodMap, MethodProduct
from transactron.lib import BasicFifo
from transactron.lib.metrics import HwMetricsEnabledKey

__all__ = ["Core"]


class Core(Elaboratable):
    def __init__(self, *, gen_params: GenParams, wb_instr_bus: WishboneInterface, wb_data_bus: WishboneInterface):
        self.gen_params = gen_params

        dep_manager = DependencyContext.get()
        if self.gen_params.debug_signals_enabled:
            dep_manager.add_dependency(HwMetricsEnabledKey(), True)

        self.wb_instr_bus = wb_instr_bus
        self.wb_data_bus = wb_data_bus

        self.wb_master_instr = WishboneMaster(self.gen_params.wb_params, "instr")
        self.wb_master_data = WishboneMaster(self.gen_params.wb_params, "data")

        self.bus_master_instr_adapter = WishboneMasterAdapter(self.wb_master_instr)
        self.bus_master_data_adapter = WishboneMasterAdapter(self.wb_master_data)

        self.core_counter = CoreInstructionCounter(self.gen_params)

        # make fetch_continue visible outside the core for injecting instructions
        self.fifo_fetch = FIFO(self.gen_params.get(FetchLayouts).raw_instr, 2)

        drop_args_transform = (self.gen_params.get(FetchLayouts).raw_instr, lambda _a, _b: {})
        self.core_counter_increment_discard_map = MethodMap(
            self.core_counter.increment, i_transform=drop_args_transform
        )
        self.fetch_continue = MethodProduct([self.fifo_fetch.write, self.core_counter_increment_discard_map.method])

        self.free_rf_fifo = BasicFifo(
            self.gen_params.get(SchedulerLayouts).free_rf_layout, 2**self.gen_params.phys_regs_bits
        )

        cache_layouts = self.gen_params.get(ICacheLayouts)
        if gen_params.icache_params.enable:
            self.icache_refiller = SimpleCommonBusCacheRefiller(
                cache_layouts, self.gen_params.icache_params, self.bus_master_instr_adapter
            )
            self.icache = ICache(cache_layouts, self.gen_params.icache_params, self.icache_refiller)
        else:
            self.icache = ICacheBypass(cache_layouts, gen_params.icache_params, self.bus_master_instr_adapter)

        self.FRAT = FRAT(gen_params=self.gen_params)
        self.RRAT = RRAT(gen_params=self.gen_params)
        self.RF = RegisterFile(gen_params=self.gen_params)
        self.ROB = ReorderBuffer(gen_params=self.gen_params)

        self.connections = gen_params.get(DependencyManager)
        self.connections.add_dependency(CommonBusDataKey(), self.bus_master_data_adapter)
        self.connections.add_dependency(FlushICacheKey(), self.icache.flush)

        self.fetch = FetchUnit(self.gen_params, self.icache, self.fetch_continue.method)

        self.exception_cause_register = ExceptionCauseRegister(
            self.gen_params, rob_get_indices=self.ROB.get_indices, fetch_stall_exception=self.fetch.stall_exception
        )

        self.func_blocks_unifier = FuncBlocksUnifier(
            gen_params=gen_params,
            blocks=gen_params.func_units_config,
            extra_methods_required=[FetchResumeKey()],
        )

        self.announcement = ResultAnnouncement(
            gen_params=self.gen_params,
            get_result=self.func_blocks_unifier.get_result,
            rob_mark_done=self.ROB.mark_done,
            rs_update=self.func_blocks_unifier.update,
            rf_write=self.RF.write,
        )

        self.interrupt_controller = InterruptController(self.gen_params)

        self.csr_generic = GenericCSRRegisters(self.gen_params)
        self.connections.add_dependency(GenericCSRRegistersKey(), self.csr_generic)

    def elaborate(self, platform):
        m = TModule()

        connect(m, flipped(self.wb_instr_bus), self.wb_master_instr.wb_master)
        connect(m, flipped(self.wb_data_bus), self.wb_master_data.wb_master)

        m.submodules.wb_master_instr = self.wb_master_instr
        m.submodules.wb_master_data = self.wb_master_data

        m.submodules.bus_master_instr_adapter = self.bus_master_instr_adapter
        m.submodules.bus_master_data_adapter = self.bus_master_data_adapter

        m.submodules.free_rf_fifo = free_rf_fifo = self.free_rf_fifo
        m.submodules.FRAT = frat = self.FRAT
        m.submodules.RRAT = rrat = self.RRAT
        m.submodules.RF = rf = self.RF
        m.submodules.ROB = rob = self.ROB

        if self.icache_refiller:
            m.submodules.icache_refiller = self.icache_refiller
        m.submodules.icache = self.icache

        m.submodules.fetch_continue = self.fetch_continue
        m.submodules.fetch = self.fetch
        m.submodules.fifo_fetch = self.fifo_fetch
        m.submodules.core_counter = self.core_counter
        m.submodules.args_discard_map = self.core_counter_increment_discard_map

        m.submodules.fifo_decode = fifo_decode = FIFO(self.gen_params.get(DecodeLayouts).decoded_instr, 2)
        m.submodules.decode = DecodeStage(
            gen_params=self.gen_params, get_raw=self.fifo_fetch.read, push_decoded=fifo_decode.write
        )

        m.submodules.scheduler = Scheduler(
            get_instr=fifo_decode.read,
            get_free_reg=free_rf_fifo.read,
            rat_rename=frat.rename,
            rob_put=rob.put,
            rf_read1=rf.read1,
            rf_read2=rf.read2,
            reservation_stations=self.func_blocks_unifier.rs_blocks,
            gen_params=self.gen_params,
        )

        m.submodules.exception_cause_register = self.exception_cause_register

        m.submodules.fetch_resume_connector = ConnectTrans(
            self.func_blocks_unifier.get_extra_method(FetchResumeKey()), self.fetch.resume
        )

        m.submodules.announcement = self.announcement
        m.submodules.func_blocks_unifier = self.func_blocks_unifier
        m.submodules.retirement = Retirement(
            self.gen_params,
            rob_peek=rob.peek,
            rob_retire=rob.retire,
            r_rat_commit=rrat.commit,
            r_rat_peek=rrat.peek,
            free_rf_put=free_rf_fifo.write,
            rf_free=rf.free,
            exception_cause_get=self.exception_cause_register.get,
            exception_cause_clear=self.exception_cause_register.clear,
            frat_rename=frat.rename,
            fetch_continue=self.fetch.resume,
            instr_decrement=self.core_counter.decrement,
            trap_entry=self.interrupt_controller.entry,
        )

        m.submodules.interrupt_controller = self.interrupt_controller

        m.submodules.csr_generic = self.csr_generic

        # push all registers to FreeRF at reset. r0 should be skipped, stop when counter overflows to 0
        free_rf_reg = Signal(self.gen_params.phys_regs_bits, reset=1)
        with Transaction(name="InitFreeRFFifo").body(m, request=(free_rf_reg.bool())):
            free_rf_fifo.write(m, free_rf_reg)
            m.d.sync += free_rf_reg.eq(free_rf_reg + 1)

        # TODO: Remove when Branch Predictor implemented
        with Transaction(name="DiscardBranchVerify").body(m):
            read = self.connections.get_dependency(BranchVerifyKey())
            read(m)  # Consume to not block JB Unit

        return m
