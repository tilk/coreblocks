from collections.abc import Iterable

from amaranth import *

from coreblocks.params import GenParams, BlockComponentParams
from transactron import TModule

__all__ = ["FuncBlocksUnifier"]


class FuncBlocksUnifier(Elaboratable):
    def __init__(
        self,
        *,
        gen_params: GenParams,
        blocks: Iterable[BlockComponentParams],
    ):
        self.gen_params = gen_params
        self.blocks = list(blocks)

    def elaborate(self, platform):
        m = TModule()

        self.rs_blocks = [(block.get_module(self.gen_params, m), block.get_optypes()) for block in self.blocks]

        for n, (unit, _) in enumerate(self.rs_blocks):
            m.submodules[f"rs_block_{n}"] = unit

        return m
