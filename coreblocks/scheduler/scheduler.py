from collections.abc import Sequence

from amaranth import *

from amaranth.lib.data import ArrayLayout, View
from transactron import Method, Methods, Required, Transaction, TModule
from transactron.lib import Connect, Pipe, WideFifo
from transactron.lib.metrics import TaggedCounter
from transactron.lib.pipeline import PipelineBuilder
from transactron.utils import OneHotMux, logging, assign, AssignType, make_layout
from transactron.utils.amaranth_ext.data import transpose
from transactron.utils.dependencies import DependencyContext

from transactron.evlog import EventSource

from coreblocks.interface.layouts import (
    CommonLayoutFields,
    RATLayouts,
    RFLayouts,
    ROBLayouts,
    RSFullDataLayout,
    SchedulerLayouts,
)
from coreblocks.params import GenParams
from coreblocks.arch.optypes import OpType, impure_optypes
from coreblocks.interface.keys import CoreStateKey, RVVIHartCollectorKey
from coreblocks.telemetry import RobAllocate, SchedulerEnter

__all__ = ["Scheduler"]


log = logging.HardwareLogger("frontend.scheduler")
evlog = EventSource("backend.scheduler")


class Scheduler(Elaboratable):
    """
    Module responsible for preparing an instruction and its insertion into RS. It supports
    multiple RS configurations, in which case, it will send the instruction to the first
    available RS which supports this kind of instructions.

    In order to prepare instruction it performs following steps:
    - physical register allocation
    - register renaming
    - ROB entry allocation
    - RS selection
    - RS insertion

    Warnings
    --------
    Instruction without any supporting RS will get stuck and block the scheduler pipeline.
    """

    get_instr: Required[Method]
    """
    Method providing decoded instructions to be scheduled for execution. It has layout as described
    by `SchedulerLayouts.scheduler_in`.
    """

    get_free_reg: Required[Methods]
    """Provides the ID of a currently free physical register."""

    crat_commit_checkpoint: Required[Method]
    """Handles tags and checkpoints in C-RAT while renaming.."""

    crat_rename: Required[Methods]
    """Renames the source register in C-RAT."""

    crat_tag: Required[Method]
    """Tags instructions to checkpoints in C-RAT."""

    crat_active_tags: Required[Method]
    """Gets information about tags that are on current speculation path from C-RAT."""

    rob_put: Required[Method]
    """Gets a free entry in ROB."""

    rf_read_req: Required[Methods]
    """Requests value of a source register."""

    rf_read_resp: Required[Methods]
    """Gets requested value of a source register and information if it is valid."""

    rs_select: Required[Sequence[Method]]
    """Selects RS slot."""

    rs_insert: Required[Sequence[Method]]
    """Inserts instruction into RS slot."""

    def __init__(self, *, gen_params: GenParams):
        """
        Parameters
        ----------
        gen_params: GenParams
            Core generation parameters.
        """
        self.gen_params = gen_params
        self.layouts = self.gen_params.get(SchedulerLayouts)
        self.get_instr = Method(o=self.layouts.scheduler_in)
        self.get_free_reg = Methods(gen_params.frontend_superscalarity, o=self.layouts.free_rf_layout)
        self.crat_commit_checkpoint = Method(i=gen_params.get(RATLayouts).crat_commit_checkpoint_in)
        self.crat_rename = Methods(
            gen_params.frontend_superscalarity,
            i=gen_params.get(RATLayouts).crat_rename_in,
            o=gen_params.get(RATLayouts).crat_rename_out,
        )
        self.crat_active_tags = Method(o=gen_params.get(RATLayouts).get_active_tags_out)
        self.crat_tag = Method(i=gen_params.get(RATLayouts).crat_tag_in, o=gen_params.get(RATLayouts).crat_tag_out)
        self.rob_put = Method(i=gen_params.get(ROBLayouts).put_layout, o=gen_params.get(ROBLayouts).put_out_layout)
        self.rf_read_req = Methods(2 * gen_params.frontend_superscalarity, i=gen_params.get(RFLayouts).rf_read_in)
        self.rf_read_resp = Methods(
            2 * gen_params.frontend_superscalarity,
            i=gen_params.get(RFLayouts).rf_read_in,
            o=gen_params.get(RFLayouts).rf_read_out,
        )
        self.rs_select = [Method(o=conf.get_layouts(gen_params).select_out) for conf in gen_params.func_units_config]
        self.rs_insert = [Method(i=conf.get_layouts(gen_params).insert_in) for conf in gen_params.func_units_config]

        self.perf_rs_selection_count = TaggedCounter(
            "frontend.scheduler.rs_selection_count",
            description="Number of instructions inserted into RSs in one cycle",
            tags=range(gen_params.frontend_superscalarity + 1),
        )

    def elaborate(self, platform):
        m = TModule()

        fields = self.gen_params.get(CommonLayoutFields)

        pipeline1 = PipelineBuilder()
        pipeline2 = PipelineBuilder()

        m.submodules.rob_alloc_out_buf = rob_alloc_out_buf = WideFifo(
            self.layouts.rs_select_in_data,
            2 * self.gen_params.frontend_superscalarity,
            self.gen_params.frontend_superscalarity,
        )

        m.submodules += [self.perf_rs_selection_count]

        @pipeline1.stage(m)
        def _():
            return transpose(self.get_instr(m))

        @pipeline1.stage(m)
        def reg_alloc(count, ftq_ptr, ftq_offset, regs_l):
            regs_p = Signal(ArrayLayout(make_layout(fields.rp_dst), self.gen_params.frontend_superscalarity))

            for i in range(self.gen_params.frontend_superscalarity):
                evlog.emit(
                    m,
                    SchedulerEnter.hw(ftq_ptr=ftq_ptr[i], ftq_offset=ftq_offset[i]),
                    when=i < count,
                )

                with m.If((i < count) & (regs_l[i].rl_dst != 0)):
                    m.d.av_comb += regs_p[i].rp_dst.eq(self.get_free_reg[i](m).ident)

            return {"regs_p": regs_p}

        # This could be ideally changed to Connect, but unfortunately causes comb loop

        @pipeline1.stage(m)
        def instr_tag(count, rollback_tag, rollback_tag_v, commit_checkpoint):
            idx = Signal(range(self.gen_params.frontend_superscalarity))
            m.d.av_comb += idx.eq(count - 1)

            tag_out = self.crat_tag(
                m,
                rollback_tag=rollback_tag[idx],
                rollback_tag_v=rollback_tag_v[idx],
                commit_checkpoint=commit_checkpoint[idx],
            )

            # Tag increment happens after jump or flush - first insn in group
            tag_increment = transpose(
                Signal(ArrayLayout(make_layout(fields.tag_increment), self.gen_params.frontend_superscalarity))
            ).tag_increment
            m.d.av_comb += tag_increment[0].eq(tag_out.tag_increment)

            # Tag is the same for all instructions
            # Jump insn always last in group - commit_checkpoint is related to it
            return {"tag": tag_out.tag, "tag_increment": tag_increment, "commit_checkpoint": tag_out.commit_checkpoint}

        @pipeline1.stage(m)
        def renaming(count, tag, commit_checkpoint, regs_l, regs_p):
            regs_p_out = Signal(
                ArrayLayout(
                    make_layout(fields.rp_s1, fields.rp_s2, fields.rp_dst), self.gen_params.frontend_superscalarity
                )
            )

            idx = Signal(range(self.gen_params.frontend_superscalarity))
            m.d.av_comb += idx.eq(count - 1)

            # tag is the same for all instrs
            self.crat_commit_checkpoint(m, tag=tag, commit_checkpoint=commit_checkpoint)

            for i in range(self.gen_params.frontend_superscalarity):
                with m.If(i < count):
                    renamed_regs = self.crat_rename[i](
                        m,
                        rl_s1=regs_l[i].rl_s1,
                        rl_s2=regs_l[i].rl_s2,
                        rl_dst=regs_l[i].rl_dst,
                        rp_dst=regs_p[i].rp_dst,
                    )

                m.d.av_comb += regs_p_out[i].rp_dst.eq(regs_p[i].rp_dst)
                m.d.av_comb += regs_p_out[i].rp_s1.eq(renamed_regs.rp_s1)
                m.d.av_comb += regs_p_out[i].rp_s2.eq(renamed_regs.rp_s2)

            # TODO: regs_l can become smaller, maybe split it into subfields?
            return {"regs_p": regs_p_out}

        @pipeline1.stage(m)
        def rob_alloc(count, ftq_ptr, ftq_offset, regs_l, regs_p, tag_increment, exec_fn):
            rvvi = DependencyContext.get().get_optional_dependency(RVVIHartCollectorKey())

            rob_ids = self.rob_put(
                m,
                count=count,
                entries=[
                    {
                        "rob_data": {
                            "rl_dst": regs_l[i].rl_dst,
                            "rp_dst": regs_p[i].rp_dst,
                            "tag_increment": tag_increment[i],
                            "ftq_ptr": ftq_ptr[i],
                        },
                        "pure": ~Cat(exec_fn[i].op_type == op_type for op_type in impure_optypes).any(),
                    }
                    for i in range(self.gen_params.frontend_superscalarity)
                ],
            )
            rob_id = transpose(
                Signal(ArrayLayout(make_layout(fields.rob_id), self.gen_params.frontend_superscalarity))
            ).rob_id

            for i in range(self.gen_params.frontend_superscalarity):
                m.d.av_comb += rob_id[i].eq(rob_ids.entries[i].rob_id)

                evlog.emit(
                    m,
                    RobAllocate.hw(
                        ftq_ptr=ftq_ptr[i],
                        ftq_offset=ftq_offset[i],
                        rob_id=rob_ids.entries[i].rob_id,
                    ),
                    when=i < count,
                )

                if rvvi is not None:
                    with m.If(i < count):
                        rvvi.register_ftq_rob_assoc[i](
                            m,
                            rob_id=rob_ids.entries[i].rob_id,
                            ftq_ptr=ftq_ptr[i],
                            ftq_offset=ftq_offset[i],
                        )

            return {"rob_id": rob_id}

        @pipeline1.stage(m)
        def _():
            # TODO transpose again
            rob_alloc_out_buf.write(m)

        @pipeline2.stage(m)
        def rs_selection():
            count = Signal(range(self.gen_params.frontend_superscalarity + 1))
            data_out = Signal(self.layouts.rs_select_out)
            instrs = rob_alloc_out_buf.peek(m)
            m.d.av_comb += data_out.count.eq(count)

            prev_insert: Value = C(1)

            for i in range(self.gen_params.frontend_superscalarity):
                next_insert = Signal()
                instr = instrs.data[i]
                instr_out = data_out.data[i]
                lookup = Signal(OpType)  # lookup of currently processed optype
                m.d.av_comb += lookup.eq(instr.exec_fn.op_type)
                m.d.av_comb += assign(instr_out, instr)
                optype_matches_list: list[Value] = []

                for j, (alloc, block_params) in enumerate(zip(self.rs_select, self.gen_params.func_units_config)):
                    # checks if RS can perform this kind of operation
                    optype_matches = Cat(lookup == op for op in block_params.get_optypes()).any()
                    optype_matches_list.append(optype_matches)
                    tr = Transaction(name=f"RSSelection_{i}_{j}")
                    with tr.body(m, ready=(i < instrs.count) & prev_insert & optype_matches):
                        # Transactron guarantees each RS will only be allocated once
                        allocated_field = alloc(m)

                        m.d.comb += instr_out.rs_entry_id.eq(allocated_field.rs_entry_id)
                        m.d.comb += instr_out.rs_selected.eq(j)

                        m.d.comb += next_insert.eq(1)

                        self.rf_read_req[2 * i](m, instr.regs_p.rp_s1)
                        self.rf_read_req[2 * i + 1](m, instr.regs_p.rp_s2)

                with m.If(i < instrs.count):
                    log.assertion(m, Cat(optype_matches_list).any(), "optype {} didn't match any RS", lookup)

                with m.If(next_insert):
                    m.d.av_comb += count.eq(i + 1)

                prev_insert = next_insert

            rob_alloc_out_buf.read(m, count=count)
            self.perf_rs_selection_count.incr(m, tag=count)
            return data_out

        @pipeline2.stage(m)
        def rs_insertion(m, count, data):
            # when core is flushed, rp_dst are discarded.
            # source operands may never become ready, skip waiting for them in any in RSes/FBs.
            # it could happen when rp is already announced and freed in RF due to flushing, but remaining instructions
            # could still depend on it.

            core_state = DependencyContext.get().get_dependency(CoreStateKey())
            flushing = core_state(m).flushing

            active_tags = self.crat_active_tags(m)
            rs_entry_id: list[Value] = []
            rs_selected: list[Value] = []
            rs_datas: list[View] = []

            for i in range(self.gen_params.frontend_superscalarity):
                instr = data[i]
                tag_inactive = ~active_tags.active_tags[instr.tag]
                skip_source_registers = flushing | tag_inactive

                # RS insertion guarantees RF response for present instructions
                # Nested transaction used to avoid locking
                with (tr := Transaction()).body(m):
                    source1 = self.rf_read_resp[2 * i](m, reg_id=instr.regs_p.rp_s1)
                    source2 = self.rf_read_resp[2 * i + 1](m, reg_id=instr.regs_p.rp_s2)
                log.assertion(m, tr.run == (i < count), f"invalid RF response for instr {i}")

                rs_data = Signal(self.gen_params.get(RSFullDataLayout).data_layout)
                m.d.av_comb += assign(
                    rs_data,
                    {
                        # when operand value is valid the convention is to set operand source to 0
                        "rp_s1": Mux(source1.valid | skip_source_registers, 0, instr.regs_p.rp_s1),
                        "rp_s2": Mux(source2.valid | skip_source_registers, 0, instr.regs_p.rp_s2),
                        "rp_s1_reg": instr.regs_p.rp_s1,
                        "rp_s2_reg": instr.regs_p.rp_s2,
                        "rp_dst": instr.regs_p.rp_dst,
                        "rob_id": instr.rob_id,
                        "exec_fn": instr.exec_fn,
                        "s1_val": Mux(source1.valid, source1.reg_val, 0),
                        "s2_val": Mux(source2.valid, source2.reg_val, 0),
                        "imm": instr.imm,
                        "csr": instr.csr,
                        "pc": instr.pc,
                        "tag": instr.tag,
                        "ftq_ptr": instr.ftq_ptr,
                    },
                )
                rs_datas.append(rs_data)
                rs_entry_id.append(instr.rs_entry_id)
                rs_selected.append(instr.rs_selected)

            for j, rs_insert in enumerate(self.rs_insert):
                # because instrs come from RS selection, this is guaranteed one-hot or zero
                matches = Signal(self.gen_params.frontend_superscalarity)
                m.d.av_comb += matches.eq(
                    Cat((i < count) & (rs_selected[i] == j) for i in range(self.gen_params.frontend_superscalarity))
                )

                matched_rs_data = OneHotMux.create(m, [(matches[i], rs_datas[i]) for i in range(len(matches))])
                matched_entry_id = OneHotMux.create(m, [(matches[i], rs_entry_id[i]) for i in range(len(matches))])

                arg = Signal.like(rs_insert.data_in)
                # connect only matching fields
                m.d.av_comb += assign(arg.rs_data, matched_rs_data, fields=AssignType.COMMON)
                # this assignment truncates signal width from max rs_entry_bits to target RS specific width
                m.d.av_comb += arg.rs_entry_id.eq(matched_entry_id)

                with m.If(matches.any()):
                    rs_insert(m, arg)

        return m
