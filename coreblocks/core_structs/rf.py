from functools import reduce
from operator import or_
from amaranth import *
from amaranth.lib.data import ArrayLayout
from transactron import Method, Methods, Transaction, def_method, TModule, def_methods
from transactron.utils import assign
from coreblocks.interface.layouts import RFLayouts
from coreblocks.params import GenParams
from transactron.lib.metrics import HwExpHistogram, TaggedLatencyMeasurer
from transactron.lib.storage import MemoryBank
from transactron.utils.amaranth_ext.functions import popcount

__all__ = ["RegisterFile"]


class RegisterFile(Elaboratable):
    def __init__(self, *, gen_params: GenParams, num_bypass: int = 0):
        self.gen_params = gen_params
        layouts = gen_params.get(RFLayouts)
        self.read_layout = layouts.rf_read_out
        self.entries = MemoryBank(
            shape=gen_params.isa.xlen,
            depth=2**gen_params.phys_regs_bits,
            read_ports=2,
            transparent=True,
        )
        self.valids = Array(Signal(init=k == 0) for k in range(2**gen_params.phys_regs_bits))

        self.read_req1 = Method(i=layouts.rf_read_in)
        self.read_req2 = Method(i=layouts.rf_read_in)
        self.read_resp1 = Method(i=layouts.rf_read_in, o=layouts.rf_read_out)
        self.read_resp2 = Method(i=layouts.rf_read_in, o=layouts.rf_read_out)
        self.write = Method(i=layouts.rf_write)
        self.bypass = Methods(num_bypass, i=layouts.rf_write)
        self.free = Method(i=layouts.rf_free)

        self.perf_rf_valid_time = TaggedLatencyMeasurer(
            "struct.rf.valid_time",
            description="Distribution of time registers are valid in RF",
            slots_number=2**gen_params.phys_regs_bits,
            max_latency=1000,
        )
        self.perf_num_valid = HwExpHistogram(
            "struct.rf.num_valid",
            description="Number of valid registers in RF",
            bucket_count=gen_params.phys_regs_bits + 1,
            sample_width=gen_params.phys_regs_bits + 1,
        )

    def elaborate(self, platform):
        m = TModule()

        m.submodules += [self.entries, self.perf_rf_valid_time, self.perf_num_valid]

        num_bypass = len(self.bypass)
        bypass_valids = Signal(num_bypass + 1)
        bypass_data = Signal(ArrayLayout(self.write.layout_in, num_bypass + 1))

        @def_method(m, self.read_req1)
        def _(reg_id: Value):
            self.entries.read_req[0](m, addr=reg_id)

        @def_method(m, self.read_req2)
        def _(reg_id: Value):
            self.entries.read_req[1](m, addr=reg_id)

        def perform_read(reg_id: Value, reg_val: Value):
            bypass_hits = Signal.like(bypass_valids)
            m.d.av_comb += bypass_hits.eq(bypass_valids & Cat(reg_id == bypass.reg_id for bypass in bypass_data))
            ret = Signal(self.read_resp1.layout_out)
            with m.If(bypass_hits.any()):
                data_bypassed = reduce(
                    or_, [Mux(bypass_hits[i], bypass.reg_val, 0) for i, bypass in enumerate(iter(bypass_data))]
                )
                m.d.av_comb += assign(ret, {"reg_val": data_bypassed, "valid": 1})
            with m.Else():
                m.d.av_comb += assign(ret, {"reg_val": reg_val, "valid": self.valids[reg_id]})
            return ret

        @def_method(m, self.read_resp1)
        def _(reg_id: Value):
            return perform_read(reg_id, self.entries.read_resp[0](m).data)

        @def_method(m, self.read_resp2)
        def _(reg_id: Value):
            return perform_read(reg_id, self.entries.read_resp[1](m).data)

        @def_method(m, self.write)
        def _(reg_id: Value, reg_val: Value):
            with m.If(reg_id != 0):
                m.d.comb += bypass_valids[num_bypass].eq(1)
                m.d.av_comb += assign(bypass_data[num_bypass], {"reg_id": reg_id, "reg_val": reg_val})
                self.entries.write(m, addr=reg_id, data=reg_val)
                m.d.sync += self.valids[reg_id].eq(1)
                self.perf_rf_valid_time.start(m, slot=reg_id)

        @def_methods(m, self.bypass)
        def _(k: int, reg_id: Value, reg_val: Value):
            with m.If(reg_id != 0):
                m.d.comb += bypass_valids[k].eq(1)
                m.d.av_comb += assign(bypass_data[k], {"reg_id": reg_id, "reg_val": reg_val})

        @def_method(m, self.free)
        def _(reg_id: Value):
            with m.If(reg_id != 0):
                m.d.sync += self.valids[reg_id].eq(0)
                self.perf_rf_valid_time.stop(m, slot=reg_id)

        if self.perf_num_valid.metrics_enabled():
            num_valid = Signal(self.gen_params.phys_regs_bits + 1)
            m.d.comb += num_valid.eq(
                popcount(Cat(self.valids[reg_id] for reg_id in range(2**self.gen_params.phys_regs_bits)))
            )
            with Transaction(name="perf").body(m):
                self.perf_num_valid.add(m, num_valid)

        return m
