from amaranth import *
from amaranth.lib.data import ArrayLayout
from transactron import Method, Transaction, def_method, TModule
from transactron.lib.storage import AsyncMemoryBank
from transactron.utils import DependencyContext
from coreblocks.interface.layouts import RATLayouts
from coreblocks.interface.keys import RollbackKey
from coreblocks.params import GenParams

__all__ = ["FRAT", "RRAT", "DummyCRAT"]


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

        self.entries = AsyncMemoryBank(shape=self.gen_params.phys_regs_bits, depth=self.gen_params.isa.reg_cnt)

        layouts = gen_params.get(RATLayouts)
        self.commit = Method(i=layouts.rrat_commit_in, o=layouts.rrat_commit_out)
        self.peek = Method(i=layouts.rrat_peek_in, o=layouts.rrat_peek_out)

    def elaborate(self, platform):
        m = TModule()
        m.submodules.entries = self.entries

        initialized = Signal()
        rl_idx = Signal(range(self.gen_params.isa.reg_cnt))
        with Transaction().body(m, ready=~initialized):
            self.entries.write(m, addr=rl_idx, data=0)
            m.d.sync += rl_idx.eq(rl_idx + 1)
            with m.If(rl_idx == self.gen_params.isa.reg_cnt - 1):
                m.d.sync += initialized.eq(1)

        @def_method(m, self.commit, ready=initialized)
        def _(rp_dst: Value, rl_dst: Value):
            self.entries.write(m, addr=rl_dst, data=rp_dst)
            return {"old_rp_dst": self.entries.read(m, addr=rl_dst).data}

        @def_method(m, self.peek, ready=initialized)
        def _(rl_dst: Value):
            return self.entries.read(m, addr=rl_dst).data

        return m


class DummyCRAT(Elaboratable):
    def __init__(self, *, gen_params: GenParams):
        self.gen_params = gen_params

        layouts = gen_params.get(RATLayouts)
        self.tag = Method(i=layouts.crat_tag_in, o=layouts.crat_tag_out)
        self.rename = Method(i=layouts.crat_rename_in, o=layouts.crat_rename_out)
        self.flush_restore = Method(i=layouts.crat_flush_restore)

        self.rollback = Method(i=layouts.rollback_in)
        self.dm = DependencyContext.get()
        self.dm.add_dependency(RollbackKey(), self.rollback)

        self.free_tag = Method()
        self.get_active_tags = Method(o=layouts.get_active_tags_out)

    def elaborate(self, platform):
        m = TModule()

        m.submodules.frat = frat = FRAT(gen_params=self.gen_params)

        @def_method(m, self.rename)
        def _(rp_dst: Value, rl_dst: Value, rl_s1: Value, rl_s2: Value, tag: Value, commit_checkpoint: Value):
            return frat.rename(m, rp_dst=rp_dst, rl_dst=rl_dst, rl_s1=rl_s1, rl_s2=rl_s2)

        @def_method(m, self.flush_restore)
        def _(rl_dst: Value, rp_dst: Value):
            frat.rename(m, rp_dst=rp_dst, rl_dst=rl_dst, rl_s1=0, rl_s2=0)

        @def_method(m, self.tag)
        def _(rollback_tag: Value, rollback_tag_v: Value, commit_checkpoint: Value):
            return

        @def_method(m, self.rollback)
        def _(tag: Value):
            return

        @def_method(m, self.free_tag)
        def _():
            return

        @def_method(m, self.get_active_tags, nonexclusive=True)
        def _():
            out = Signal(ArrayLayout(1, 2**self.gen_params.tag_bits))
            m.d.av_comb += out.eq(-1)
            return {"active_tags": out}

        return m
