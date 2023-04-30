from collections import defaultdict, deque
from collections.abc import Iterable, Callable, Mapping, Iterator, Sequence
from contextlib import contextmanager
from enum import Enum, auto
from typing import ClassVar, TypeAlias, TypedDict, Union, Optional, Tuple
from graphlib import TopologicalSorter
from typing_extensions import Self
from amaranth import *
from amaranth import tracer
from amaranth.hdl.ast import Statement
from itertools import count, chain

from coreblocks.utils import AssignType, assign, ModuleConnector
from ._utils import *
from ..utils._typing import StatementLike, ValueLike, SignalBundle
from .graph import Owned, OwnershipGraph, Direction

__all__ = [
    "MethodLayout",
    "Priority",
    "TransactionManager",
    "TransactionContext",
    "TransactionModule",
    "Transaction",
    "Method",
    "eager_deterministic_cc_scheduler",
    "trivial_roundrobin_cc_scheduler",
    "def_method",
]


TransactionGraph: TypeAlias = Graph["Transaction"]
TransactionGraphCC: TypeAlias = GraphCC["Transaction"]
PriorityOrder: TypeAlias = dict["Transaction", int]
TransactionScheduler: TypeAlias = Callable[["MethodMap", TransactionGraph, TransactionGraphCC, PriorityOrder], Module]
RecordDict: TypeAlias = ValueLike | Mapping[str, "RecordDict"]
TransactionOrMethod: TypeAlias = Union["Transaction", "Method"]


class Priority(Enum):
    #: Conflicting transactions/methods don't have a priority order.
    UNDEFINED = auto()
    #: Left transaction/method is prioritized over the right one.
    LEFT = auto()
    #: Right transaction/method is prioritized over the left one.
    RIGHT = auto()


class RelationBase(TypedDict):
    end: TransactionOrMethod
    priority: Priority
    conflict: bool


class Relation(RelationBase):
    start: TransactionOrMethod


class MethodMap:
    def __init__(self, transactions: Iterable["Transaction"]):
        self.methods_by_transaction = dict[Transaction, list[Method]]()
        self.transactions_by_method = defaultdict[Method, list[Transaction]](list)

        def rec(transaction: Transaction, source: TransactionBase):
            for method in source.method_uses.keys():
                if not method.defined:
                    raise RuntimeError("Trying to use method which is not defined yet")
                if method in self.methods_by_transaction[transaction]:
                    raise RuntimeError("Method can't be called twice from the same transaction")
                self.methods_by_transaction[transaction].append(method)
                self.transactions_by_method[method].append(transaction)
                rec(transaction, method)

        for transaction in transactions:
            self.methods_by_transaction[transaction] = []
            rec(transaction, transaction)

    def transactions_for(self, elem: TransactionOrMethod) -> Iterable["Transaction"]:
        if isinstance(elem, Transaction):
            return [elem]
        else:
            return self.transactions_by_method[elem]

    @property
    def methods(self) -> Iterable["Method"]:
        return self.transactions_by_method.keys()

    @property
    def transactions(self) -> Iterable["Transaction"]:
        return self.methods_by_transaction.keys()

    @property
    def methods_and_transactions(self) -> Iterable[TransactionOrMethod]:
        return chain(self.methods, self.transactions)


def eager_deterministic_cc_scheduler(
    method_map: MethodMap, gr: TransactionGraph, cc: TransactionGraphCC, porder: PriorityOrder
) -> Module:
    """eager_deterministic_cc_scheduler

    This function generates an eager scheduler for the transaction
    subsystem. It isn't fair, because it starts transactions using
    transaction index in `cc` as a priority. Transaction with the lowest
    index has the highest priority.

    If there are two different transactions which have no conflicts then
    they will be started concurrently.

    Parameters
    ----------
    manager : TransactionManager
        TransactionManager which uses this instance of scheduler for
        arbitrating which agent should get a grant signal.
    m : Module
        Module to which signals and calculations should be connected.
    gr : TransactionGraph
        Graph of conflicts between transactions, where vertices are transactions and edges are conflicts.
    cc : Set[Transaction]
        Connected components of the graph `gr` for which scheduler
        should be generated.
    porder : PriorityOrder
        Linear ordering of transactions which is consistent with priority constraints.
    """
    m = Module()
    ccl = list(cc)
    ccl.sort(key=lambda transaction: porder[transaction])
    for k, transaction in enumerate(ccl):
        ready = [method.ready for method in method_map.methods_by_transaction[transaction]]
        runnable = Cat(ready).all()
        conflicts = [ccl[j].grant for j in range(k) if ccl[j] in gr[transaction]]
        noconflict = ~Cat(conflicts).any()
        m.d.comb += transaction.grant.eq(transaction.request & runnable & noconflict)
    return m


def trivial_roundrobin_cc_scheduler(
    method_map: MethodMap, gr: TransactionGraph, cc: TransactionGraphCC, porder: PriorityOrder
) -> Module:
    """trivial_roundrobin_cc_scheduler

    This function generates a simple round-robin scheduler for the transaction
    subsystem. In a one cycle there will be at most one transaction granted
    (in a given connected component of the conflict graph), even if there is
    another ready, non-conflicting, transaction. It is mainly for testing
    purposes.

    Parameters
    ----------
    manager : TransactionManager
        TransactionManager which uses this instance of scheduler for
        arbitrating which agent should get grant signal.
    m : Module
        Module to which signals and calculations should be connected.
    gr : TransactionGraph
        Graph of conflicts between transactions, where vertices are transactions and edges are conflicts.
    cc : Set[Transaction]
        Connected components of the graph `gr` for which scheduler
        should be generated.
    porder : PriorityOrder
        Linear ordering of transactions which is consistent with priority constraints.
    """
    m = Module()
    sched = Scheduler(len(cc))
    m.submodules.scheduler = sched
    for k, transaction in enumerate(cc):
        methods = method_map.methods_by_transaction[transaction]
        ready = Signal(len(methods))
        for n, method in enumerate(methods):
            m.d.comb += ready[n].eq(method.ready)
        runnable = ready.all()
        m.d.comb += sched.requests[k].eq(transaction.request & runnable)
        m.d.comb += transaction.grant.eq(sched.grant[k] & sched.valid)
    return m


class TransactionManager(Elaboratable):
    """Transaction manager

    This module is responsible for granting `Transaction`\\s and running
    `Method`\\s. It takes care that two conflicting `Transaction`\\s
    are never granted in the same clock cycle.
    """

    def __init__(self, cc_scheduler: TransactionScheduler = eager_deterministic_cc_scheduler):
        self.transactions: list[Transaction] = []
        self.cc_scheduler = cc_scheduler

    def add_transaction(self, transaction: "Transaction"):
        self.transactions.append(transaction)

    @staticmethod
    def _conflict_graph(
        method_map: MethodMap, relations: list[Relation]
    ) -> Tuple[TransactionGraph, TransactionGraph, PriorityOrder]:
        """_conflict_graph

        This function generates the graph of transaction conflicts. Conflicts
        between transactions can be explicit or implicit. Two transactions
        conflict explicitly, if a conflict was added between the transactions
        or the methods used by them via `add_conflict`. Two transactions
        conflict implicitly if they are both using the same method.

        Created graph is undirected. Transactions are nodes in that graph
        and conflict between two transactions is marked as an edge. In such
        representation connected components are sets of transactions which can
        potentially conflict so there is a need to arbitrate between them.
        On the other hand when two transactions are in different connected
        components, then they can be scheduled independently, because they
        will have no conflicts.

        This function also computes a linear ordering of transactions
        which is consistent with conflict priorities of methods and
        transactions. When priority constraints cannot be satisfied,
        an exception is thrown.

        Returns
        -------
        cgr : TransactionGraph
            Graph of conflicts between transactions, where vertices are transactions and edges are conflicts.
        rgr : TransactionGraph
            Graph of relations between transactions, which includes conflicts and orderings.
        porder : PriorityOrder
            Linear ordering of transactions which is consistent with priority constraints.
        """

        cgr: TransactionGraph = {}  # Conflict graph
        pgr: TransactionGraph = {}  # Priority graph
        rgr: TransactionGraph = {}  # Relation graph

        def add_edge(begin: Transaction, end: Transaction, priority: Priority, conflict: bool):
            rgr[begin].add(end)
            rgr[end].add(begin)
            if conflict:
                cgr[begin].add(end)
                cgr[end].add(begin)
            match priority:
                case Priority.LEFT:
                    pgr[end].add(begin)
                case Priority.RIGHT:
                    pgr[begin].add(end)

        for transaction in method_map.transactions:
            cgr[transaction] = set()
            pgr[transaction] = set()
            rgr[transaction] = set()

        for method in method_map.methods:
            if method.nonexclusive:
                continue
            for transaction1 in method_map.transactions_for(method):
                for transaction2 in method_map.transactions_for(method):
                    if transaction1 is not transaction2:
                        add_edge(transaction1, transaction2, Priority.UNDEFINED, True)

        for relation in relations:
            start = relation["start"]
            end = relation["end"]
            if not relation["conflict"]:  # relation added with schedule_before
                if end.def_order < start.def_order:
                    raise RuntimeError(f"{start.name!r} scheduled before {end.name!r}, but defined afterwards")

            for trans_start in method_map.transactions_for(start):
                for trans_end in method_map.transactions_for(end):
                    add_edge(trans_start, trans_end, relation["priority"], relation["conflict"])

        porder: PriorityOrder = {}

        for k, transaction in enumerate(TopologicalSorter(pgr).static_order()):
            porder[transaction] = k

        return cgr, rgr, porder

    @staticmethod
    def _method_uses(method_map: MethodMap) -> Mapping["Transaction", Mapping["Method", Tuple[ValueLike, ValueLike]]]:
        method_uses = defaultdict[Transaction, dict[Method, Tuple[ValueLike, ValueLike]]](dict)

        for source in method_map.methods_and_transactions:
            for transaction in method_map.transactions_for(source):
                for method, use_data in source.method_uses.items():
                    method_uses[transaction][method] = use_data

        return method_uses

    def _simultaneous(self):
        method_map = MethodMap(self.transactions)

        # step 1: conflict set generation
        conflicts = list[set[frozenset[Transaction]]]()

        def conflict_for(elems: Iterable[TransactionOrMethod]):
            elem_list = list(elems)
            sim_list = list[Transaction]()

            def rec(idx: int) -> Iterable[frozenset[Transaction]]:
                if idx == len(elem_list):
                    yield frozenset(sim_list)
                else:
                    for transaction in method_map.transactions_for(elem_list[idx]):
                        sim_list.append(transaction)
                        yield from rec(idx + 1)
                        sim_list.pop()

            return set(rec(0))

        for elem in method_map.methods_and_transactions:
            for sim in elem.simultaneous:
                conflict = conflict_for(chain([elem], sim))
                conflicts.append(conflict)

        # step 2: transitivity computation
        simultaneous = set[frozenset[Transaction]]().union(*conflicts)

        def conflicting(group1: frozenset[Transaction], group2: frozenset[Transaction]):
            for conflict in conflicts:
                if group1 in conflict and group2 in conflict:
                    return True
            return False

        def mergable_pairs(group1: frozenset[Transaction]):
            for group2 in simultaneous:
                if group1 & group2 and not conflicting(group1, group2):
                    yield (group1, group2)

        q = deque[Tuple[frozenset[Transaction], frozenset[Transaction]]]()
        for group in simultaneous:
            q.extend(mergable_pairs(group))

        while q:
            (group1, group2) = q.popleft()
            new_group = group1 | group2
            if new_group in simultaneous:
                continue
            q.extend(mergable_pairs(new_group))
            simultaneous.add(new_group)
            for conflict in conflicts:
                if group1 in conflict or group2 in conflict:
                    conflict.add(new_group)

        # step 3: maximal group selection
        def maximal(group: frozenset[Transaction]):
            return not any(group.issubset(group2) and group != group2 for group2 in simultaneous)

        final_simultaneous = set(filter(maximal, simultaneous))

        # step 4: convert transactions to methods
        joined_transactions = set[Transaction]().union(*final_simultaneous)

        self.transactions = list(filter(lambda t: t not in joined_transactions, self.transactions))
        methods = dict[Transaction, Method]()

        for transaction in joined_transactions:
            # TODO: some simpler way?
            method = Method(name=transaction.name)
            method.owner = transaction.owner
            method.ready = transaction.request
            method.run = transaction.grant
            method.defined = transaction.defined
            methods[transaction] = method

        # step 5: construct merged transactions
        m = Module()
        m._MustUse__silence = True  # type: ignore

        for group in final_simultaneous:
            with Transaction(manager=self).body(m):
                for transaction in group:
                    methods[transaction](m)

        return m

    def elaborate(self, platform):
        merge_manager = self._simultaneous()

        method_map = MethodMap(self.transactions)
        relations = [
            Relation(**relation, start=elem)
            for elem in method_map.methods_and_transactions
            for relation in elem.relations
        ]
        cgr, rgr, porder = TransactionManager._conflict_graph(method_map, relations)

        print(method_map.transactions_by_method)
        print(method_map.methods_by_transaction)

        m = Module()
        m.submodules.merge_manager = merge_manager

        m.submodules._transactron_schedulers = ModuleConnector(
            *[self.cc_scheduler(method_map, cgr, cc, porder) for cc in _graph_ccs(rgr)]
        )

        method_uses = self._method_uses(method_map)

        for method, transactions in method_map.transactions_by_method.items():
            granted = Signal(len(transactions))
            for n, transaction in enumerate(transactions):
                (tdata, enable) = method_uses[transaction][method]
                m.d.comb += granted[n].eq(transaction.grant & enable)

                with m.If(transaction.grant):
                    m.d.comb += method.data_in.eq(tdata)
            runnable = granted.any()
            m.d.comb += method.run.eq(runnable)

        return m

    def visual_graph(self, fragment):
        graph = OwnershipGraph(fragment)
        method_map = MethodMap(self.transactions)
        for method, transactions in method_map.transactions_by_method.items():
            if len(method.data_in) > len(method.data_out):
                direction = Direction.IN
            elif len(method.data_in) < len(method.data_out):
                direction = Direction.OUT
            else:
                direction = Direction.INOUT
            graph.insert_node(method)
            for transaction in transactions:
                graph.insert_node(transaction)
                graph.insert_edge(transaction, method, direction)

        return graph


class TransactionContext:
    stack: list[TransactionManager] = []

    def __init__(self, manager: TransactionManager):
        self.manager = manager

    def __enter__(self):
        self.stack.append(self.manager)
        return self

    def __exit__(self, exc_type, exc_value, exc_tb):
        top = self.stack.pop()
        assert self.manager is top

    @classmethod
    def get(cls) -> TransactionManager:
        if not cls.stack:
            raise RuntimeError("TransactionContext stack is empty")
        return cls.stack[-1]


class TransactionModule(Elaboratable):
    """
    `TransactionModule` is used as wrapper on `Module` class,
    which add support for transaction to the `Module`. It creates a
    `TransactionManager` which will handle transaction scheduling
    and can be used in definition of `Method`\\s and `Transaction`\\s.
    """

    def __init__(self, module: Module, manager: Optional[TransactionManager] = None):
        """
        Parameters
        ----------
        module: Module
                The `Module` which should be wrapped to add support for
                transactions and methods.
        """
        if manager is None:
            manager = TransactionManager()
        self.transactionManager = manager
        self.module = module

    def transaction_context(self) -> TransactionContext:
        return TransactionContext(self.transactionManager)

    def elaborate(self, platform):
        with self.transaction_context():
            for name in self.module._named_submodules:  # type: ignore
                self.module._named_submodules[name] = Fragment.get(self.module._named_submodules[name], platform)
            for idx in range(len(self.module._anon_submodules)):
                self.module._anon_submodules[idx] = Fragment.get(self.module._anon_submodules[idx], platform)

        self.module.submodules._transactron_transManager = self.transactionManager

        return self.module


class _TransactionBaseStatements:
    def __init__(self):
        self.statements: list[Statement] = []

    def __iadd__(self, assigns: StatementLike):
        if not TransactionBase.stack:
            raise RuntimeError("No current body")
        for stmt in Statement.cast(assigns):
            self.statements.append(stmt)
        return self

    def __iter__(self):
        return self.statements.__iter__()

    def clear(self):
        return self.statements.clear()


class TransactionBase(Owned):
    stack: ClassVar[list[Union["Transaction", "Method"]]] = []
    comb: ClassVar[_TransactionBaseStatements] = _TransactionBaseStatements()
    def_counter: ClassVar[count] = count()
    def_order: int
    defined: bool = False

    def __init__(self):
        self.method_uses: dict[Method, Tuple[ValueLike, ValueLike]] = dict()
        self.relations: list[RelationBase] = []
        self.simultaneous: list[Sequence[TransactionOrMethod]] = []

    def add_conflict(self, end: TransactionOrMethod, priority: Priority = Priority.UNDEFINED) -> None:
        """Registers a conflict.

        Record that that the given `Transaction` or `Method` cannot execute
        simultaneously with this `Method` or `Transaction`. Typical reason
        is using a common resource (register write or memory port).

        Parameters
        ----------
        end: Transaction or Method
            The conflicting `Transaction` or `Method`
        priority: Priority, optional
            Is one of conflicting `Transaction`\\s or `Method`\\s prioritized?
            Defaults to undefined priority relation.
        """
        self.relations.append(RelationBase(end=end, priority=priority, conflict=True))

    def schedule_before(self, end: TransactionOrMethod) -> None:
        """Adds a priority relation.

        Record that that the given `Transaction` or `Method` needs to be
        scheduled before this `Method` or `Transaction`, without adding
        a conflict. Typical reason is data forwarding.

        Parameters
        ----------
        end: Transaction or Method
            The other `Transaction` or `Method`
        """
        self.relations.append(RelationBase(end=end, priority=Priority.LEFT, conflict=False))

    def use_method(self, method: "Method", arg: ValueLike, enable: ValueLike):
        if method in self.method_uses:
            raise RuntimeError("Method can't be called twice from the same transaction")
        self.method_uses[method] = (arg, enable)

    def simultaneous_with(self, *others: TransactionOrMethod) -> None:
        self.simultaneous.append(others)

    @contextmanager
    def context(self, m: Module) -> Iterator[Self]:
        assert isinstance(self, Transaction) or isinstance(self, Method)  # for typing

        parent = TransactionBase.peek()
        if parent is None:
            assert not TransactionBase.comb.statements
        else:
            parent.schedule_before(self)

        TransactionBase.stack.append(self)

        try:
            yield self
        finally:
            TransactionBase.stack.pop()
            if parent is None:
                m.d.comb += TransactionBase.comb
                TransactionBase.comb.clear()

    @classmethod
    def get(cls) -> Self:
        ret = cls.peek()
        if ret is None:
            raise RuntimeError("No current body")
        return ret

    @classmethod
    def peek(cls) -> Optional[Self]:
        if not TransactionBase.stack:
            return None
        if not isinstance(TransactionBase.stack[-1], cls):
            raise RuntimeError(f"Current body not a {cls.__name__}")
        return TransactionBase.stack[-1]


class Transaction(TransactionBase):
    """Transaction.

    A `Transaction` represents a task which needs to be regularly done.
    Execution of a `Transaction` always lasts a single clock cycle.
    A `Transaction` signals readiness for execution by setting the
    `request` signal. If the conditions for its execution are met, it
    can be granted by the `TransactionManager`.

    A `Transaction` can, as part of its execution, call a number of
    `Method`\\s. A `Transaction` can be granted only if every `Method`
    it runs is ready.

    A `Transaction` cannot execute concurrently with another, conflicting
    `Transaction`. Conflicts between `Transaction`\\s are either explicit
    or implicit. An explicit conflict is added using the `add_conflict`
    method. Implicit conflicts arise between pairs of `Transaction`\\s
    which use the same `Method`.

    A module which defines a `Transaction` should use `body` to
    describe used methods and the transaction's effect on the module state.
    The used methods should be called inside the `body`'s
    `with` block.

    Attributes
    ----------
    name: str
        Name of this `Transaction`.
    request: Signal, in
        Signals that the transaction wants to run. If omitted, the transaction
        is always ready. Defined in the constructor.
    grant: Signal, out
        Signals that the transaction is granted by the `TransactionManager`,
        and all used methods are called.
    """

    def __init__(self, *, name: Optional[str] = None, manager: Optional[TransactionManager] = None):
        """
        Parameters
        ----------
        name: str or None
            Name hint for this `Transaction`. If `None` (default) the name is
            inferred from the variable name this `Transaction` is assigned to.
            If the `Transaction` was not assigned, the name is inferred from
            the class name where the `Transaction` was constructed.
        manager: TransactionManager
            The `TransactionManager` controlling this `Transaction`.
            If omitted, the manager is received from `TransactionContext`.
        """
        super().__init__()
        self.owner, owner_name = get_caller_class_name(default="$transaction")
        self.name = name or tracer.get_var_name(depth=2, default=owner_name)
        if manager is None:
            manager = TransactionContext.get()
        manager.add_transaction(self)
        self.request = Signal()
        self.grant = Signal()

    @contextmanager
    def body(self, m: Module, *, request: ValueLike = C(1)) -> Iterator["Transaction"]:
        """Defines the `Transaction` body.

        This context manager allows to conveniently define the actions
        performed by a `Transaction` when it's granted. Each assignment
        added to a domain under `body` is guarded by the `grant` signal.
        Combinational assignments which do not need to be guarded by
        `grant` can be added to `Transaction.comb` instead of
        `m.d.comb`. `Method` calls can be performed under `body`.

        Parameters
        ----------
        m: Module
            The module where the `Transaction` is defined.
        request: Signal
            Indicates that the `Transaction` wants to be executed. By
            default it is `Const(1)`, so it wants to be executed in
            every clock cycle.
        """
        if self.defined:
            raise RuntimeError("Transaction already defined")
        self.def_order = next(TransactionBase.def_counter)
        m.d.comb += self.request.eq(request)
        with self.context(m):
            with m.If(self.grant):
                yield self
        self.defined = True

    def __repr__(self) -> str:
        return "(transaction {})".format(self.name)

    def debug_signals(self) -> SignalBundle:
        return [self.request, self.grant]


class Method(TransactionBase):
    """Transactional method.

    A `Method` serves to interface a module with external `Transaction`\\s
    or `Method`\\s. It can be called by at most once in a given clock cycle.
    When a given `Method` is required by multiple `Transaction`\\s
    (either directly, or indirectly via another `Method`) simultenaously,
    at most one of them is granted by the `TransactionManager`, and the rest
    of them must wait. (Non-exclusive methods are an exception to this
    behavior.) Calling a `Method` always takes a single clock cycle.

    Data is combinationally transferred between to and from `Method`\\s
    using Amaranth `Record`\\s. The transfer can take place in both directions
    at the same time: from the called `Method` to the caller (`data_out`)
    and from the caller to the called `Method` (`data_in`).

    A module which defines a `Method` should use `body` or `def_method`
    to describe the method's effect on the module state.

    Attributes
    ----------
    name: str
        Name of this `Method`.
    ready: Signal, in
        Signals that the method is ready to run in the current cycle.
        Typically defined by calling `body`.
    run: Signal, out
        Signals that the method is called in the current cycle by some
        `Transaction`. Defined by the `TransactionManager`.
    data_in: Record, out
        Contains the data passed to the `Method` by the caller
        (a `Transaction` or another `Method`).
    data_out: Record, in
        Contains the data passed from the `Method` to the caller
        (a `Transaction` or another `Method`). Typically defined by
        calling `body`.
    """

    def __init__(
        self, *, name: Optional[str] = None, i: MethodLayout = (), o: MethodLayout = (), nonexclusive: bool = False
    ):
        """
        Parameters
        ----------
        name: str or None
            Name hint for this `Method`. If `None` (default) the name is
            inferred from the variable name this `Method` is assigned to.
        i: record layout
            The format of `data_in`.
            An `int` corresponds to a `Record` with a single `data` field.
        o: record layout
            The format of `data_in`.
            An `int` corresponds to a `Record` with a single `data` field.
        nonexclusive: bool
            If true, the method is non-exclusive: it can be called by multiple
            transactions in the same clock cycle. If such a situation happens,
            the method still is executed only once, and each of the callers
            receive its output. Nonexclusive methods cannot have inputs.
        """
        super().__init__()
        self.owner, owner_name = get_caller_class_name(default="$method")
        self.name = name or tracer.get_var_name(depth=2, default=owner_name)
        self.ready = Signal()
        self.run = Signal()
        self.data_in = Record(i)
        self.data_out = Record(o)
        self.nonexclusive = nonexclusive
        if nonexclusive:
            assert len(self.data_in) == 0

    @staticmethod
    def like(other: "Method", *, name: Optional[str] = None) -> "Method":
        """Constructs a new `Method` based on another.

        The returned `Method` has the same input/output data layouts as the
        `other` `Method`.

        Parameters
        ----------
        other : Method
            The `Method` which serves as a blueprint for the new `Method`.
        name : str, optional
            Name of the new `Method`.

        Returns
        -------
        Method
            The freshly constructed `Method`.
        """
        return Method(name=name, i=other.data_in.layout, o=other.data_out.layout)

    def proxy(self, m: Module, method: "Method"):
        """Define as a proxy for another method.

        The calls to this method will be forwarded to `method`.

        Parameters
        ----------
        m : Module
            Module in which operations on signals should be executed,
            `proxy` uses the combinational domain only.
        method : Method
            Method for which this method is a proxy for.
        """
        m.d.comb += self.ready.eq(1)
        m.d.comb += self.data_out.eq(method.data_out)
        self.use_method(method, arg=self.data_in, enable=self.run)
        self.defined = True

    @contextmanager
    def body(self, m: Module, *, ready: ValueLike = C(1), out: ValueLike = C(0, 0)) -> Iterator[Record]:
        """Define method body

        The `body` context manager can be used to define the actions
        performed by a `Method` when it's run. Each assignment added to
        a domain under `body` is guarded by the `run` signal.
        Combinational assignments which do not need to be guarded by `run`
        can be added to `Method.comb` instead of `m.d.comb`. `Method`
        calls can be performed under `body`.

        Parameters
        ----------
        m : Module
            Module in which operations on signals should be executed,
            `body` uses the combinational domain only.
        ready : Signal, in
            Signal to indicate if the method is ready to be run. By
            default it is `Const(1)`, so the method is always ready.
            Assigned combinationially to the `ready` attribute.
        out : Record, in
            Data generated by the `Method`, which will be passed to
            the caller (a `Transaction` or another `Method`). Assigned
            combinationally to the `data_out` attribute.

        Returns
        -------
        data_in : Record, out
            Data passed from the caller (a `Transaction` or another
            `Method`) to this `Method`.

        Examples
        --------
        .. highlight:: python
        .. code-block:: python

            m = Module()
            my_sum_method = Method(i = Layout([("arg1",8),("arg2",8)]))
            sum = Signal(16)
            with my_sum_method.body(m, out = sum) as data_in:
                m.d.comb += sum.eq(data_in.arg1 + data_in.arg2)
        """
        if self.defined:
            raise RuntimeError("Method already defined")
        self.def_order = next(TransactionBase.def_counter)
        try:
            m.d.comb += self.ready.eq(ready)
            m.d.comb += self.data_out.eq(out)
            with self.context(m):
                with m.If(self.run):
                    yield self.data_in
        finally:
            self.defined = True

    def __call__(
        self, m: Module, arg: Optional[RecordDict] = None, enable: ValueLike = C(1), /, **kwargs: RecordDict
    ) -> Record:
        """Call a method.

        Methods can only be called from transaction and method bodies.
        Calling a `Method` marks, for the purpose of transaction scheduling,
        the dependency between the calling context and the called `Method`.
        It also connects the method's inputs to the parameters and the
        method's outputs to the return value.

        Parameters
        ----------
        m : Module
            Module in which operations on signals should be executed,
        arg : Value or dict of Values
            Call argument. Can be passed as a `Record` of the method's
            input layout or as a dictionary. Alternative syntax uses
            keyword arguments.
        enable : Value
            Configures the call as enabled in the current clock cycle.
            Disabled calls still lock the called method in transaction
            scheduling. Calls are by default enabled.
        **kwargs : Value or dict of Values
            Allows to pass method arguments using keyword argument
            syntax. Equivalent to passing a dict as the argument.

        Returns
        -------
        data_out : Record
            The result of the method call.

        Examples
        --------
        .. highlight:: python
        .. code-block:: python

            m = Module()
            with Transaction.body(m):
                ret = my_sum_method(m, arg1=2, arg2=3)

        Alternative syntax:

        .. highlight:: python
        .. code-block:: python

            with Transaction.body(m):
                ret = my_sum_method(m, {"arg1": 2, "arg2": 3})
        """
        enable_sig = Signal()
        arg_rec = Record.like(self.data_in)

        if arg is not None and kwargs:
            raise ValueError("Method call with both keyword arguments and legacy record argument")

        if arg is None:
            arg = kwargs

        m.d.comb += enable_sig.eq(enable)
        TransactionBase.comb += assign(arg_rec, arg, fields=AssignType.ALL)
        TransactionBase.get().use_method(self, arg_rec, enable_sig)

        return self.data_out

    def __repr__(self) -> str:
        return "(method {})".format(self.name)

    def debug_signals(self) -> SignalBundle:
        return [self.ready, self.run, self.data_in, self.data_out]


def def_method(m: Module, method: Method, ready: ValueLike = C(1)):
    """Define a method.

    This decorator allows to define transactional methods in an
    elegant way using Python's `def` syntax. Internally, `def_method`
    uses `Method.body`.

    The decorated function should take keyword arguments corresponding to the
    fields of the method's input layout. The `**kwargs` syntax is supported.
    Alternatively, it can take one argument named `arg`, which will be a
    record with input signals.

    The returned value can be either a record with the method's output layout
    or a dictionary of outputs.

    Parameters
    ----------
    m: Module
        Module in which operations on signals should be executed.
    method: Method
        The method whose body is going to be defined.
    ready: Signal
        Signal to indicate if the method is ready to be run. By
        default it is `Const(1)`, so the method is always ready.
        Assigned combinationally to the `ready` attribute.

    Examples
    --------
    .. highlight:: python
    .. code-block:: python

        m = Module()
        my_sum_method = Method(i=[("arg1",8),("arg2",8)], o=[("res",8)])
        @def_method(m, my_sum_method)
        def _(arg1, arg2):
            return arg1 + arg2

    Alternative syntax (keyword args in dictionary):

    .. highlight:: python
    .. code-block:: python

        @def_method(m, my_sum_method)
        def _(**args):
            return args["arg1"] + args["arg2"]

    Alternative syntax (arg record):

    .. highlight:: python
    .. code-block:: python

        @def_method(m, my_sum_method)
        def _(arg):
            return {"res": arg.arg1 + arg.arg2}
    """

    def decorator(func: Callable[..., Optional[RecordDict]]):
        out = Record.like(method.data_out)
        ret_out = None

        with method.body(m, ready=ready, out=out) as arg:
            ret_out = method_def_helper(method, func, arg, **arg.fields)

        if ret_out is not None:
            m.d.comb += assign(out, ret_out, fields=AssignType.ALL)

    return decorator
