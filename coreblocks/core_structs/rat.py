from amaranth import *
from transactron import Method, Transaction, def_method, TModule
from transactron.lib.reqres import Serializer
from transactron.lib.storage import MemoryBank
from coreblocks.interface.layouts import RATLayouts
from coreblocks.params import GenParams

__all__ = ["FRAT", "RRAT"]


class FRAT(Elaboratable):
    def __init__(self, *, gen_params: GenParams):
        self.gen_params = gen_params

        self.entries = Array(Signal(self.gen_params.phys_regs_bits) for _ in range(self.gen_params.isa.reg_cnt))

        layouts = gen_params.get(RATLayouts)
        self.rename = Method(i=layouts.frat_rename_in, o=layouts.frat_rename_out)

    def elaborate(self, platform):
        m = TModule()

        @def_method(m, self.rename)
        def _(rp_dst: Value, rl_dst: Value, rl_s1: Value, rl_s2: Value):
            m.d.sync += self.entries[rl_dst].eq(rp_dst)
            return {"rp_s1": self.entries[rl_s1], "rp_s2": self.entries[rl_s2]}

        return m


class RRAT(Elaboratable):
    def __init__(self, *, gen_params: GenParams):
        self.gen_params = gen_params

        self.entries = MemoryBank(shape=self.gen_params.phys_regs_bits, depth=self.gen_params.isa.reg_cnt)

        layouts = gen_params.get(RATLayouts)
        self.commit = Method(i=layouts.rrat_commit_in)
        self.commit_result = Method(o=layouts.rrat_commit_out)
        self.peek = Method(i=layouts.rrat_peek_in)
        self.peek_result = Method(o=layouts.rrat_peek_out)

    def elaborate(self, platform):
        m = TModule()

        m.submodules.entries = self.entries
        m.submodules.serializer = serializer = Serializer(
            port_count=2,
            serialized_req_method=self.entries.read_req[0],
            serialized_resp_method=self.entries.read_resp[0],
        )

        initialized = Signal()
        rl_idx = Signal(range(self.gen_params.isa.reg_cnt))
        with Transaction().body(m, request=~initialized):
            self.entries.write(m, addr=rl_idx, data=0)
            m.d.sync += rl_idx.eq(rl_idx + 1)
            with m.If(rl_idx == self.gen_params.isa.reg_cnt - 1):
                m.d.sync += initialized.eq(1)

        @def_method(m, self.commit, ready=initialized)
        def _(rp_dst: Value, rl_dst: Value):
            self.entries.write(m, addr=rl_dst, data=rp_dst)
            serializer.serialize_in[0](m, addr=rl_dst)

        @def_method(m, self.commit_result)
        def _():
            return {"old_rp_dst": serializer.serialize_out[0](m).data}

        @def_method(m, self.peek, ready=initialized)
        def _(rl_dst: Value):
            serializer.serialize_in[1](m, addr=rl_dst)

        @def_method(m, self.peek_result)
        def _():
            return {"old_rp_dst": serializer.serialize_out[1](m).data}

        return m
