from __future__ import annotations

import itertools
import sys
from abc import ABC
from dataclasses import dataclass, field
from typing import Generic, TypeVar

if sys.version_info < (3, 8):
    from typing_extensions import Protocol
else:
    from typing import Protocol


from daft.expressions import Expression
from daft.logical import logical_plan
from daft.logical.map_partition_ops import MapPartitionOp
from daft.logical.schema import ExpressionList
from daft.runners.partitioning import PartitionMetadata, vPartition
from daft.runners.pyrunner import LocalLogicalPartitionOpRunner
from daft.runners.shuffle_ops import RepartitionHashOp, RepartitionRandomOp, SortOp

PartitionT = TypeVar("PartitionT")
ID_GEN = itertools.count()


@dataclass
class ExecutionStep(Generic[PartitionT], ABC):
    """An ExecutionStep describes a task that will run to create a partition.

    The partition will be created by running a function pipeline (`instructions`) over some input partition(s) (`inputs`).
    Each function takes an entire set of inputs and produces a new set of partitions to pass into the next function.

    This class should not be instantiated directly. See subclasses for usage:
        - OpenExecutionQueue: to create a new ExecutionStep, and append functions to its pipeline.
        - MaterializeRequest: to "freeze" an ExecutionStep and mark it for execution.
    """

    inputs: list[PartitionT]
    instructions: list[Instruction]

    def __str__(self) -> str:
        return (
            f"{self.__class__.__name__}\n"
            f"  Inputs: {self.inputs}\n"
            f"  Instructions: {[i.__class__.__name__ for i in self.instructions]}"
        )

    def __repr__(self) -> str:
        return self.__str__()


class OpenExecutionQueue(ExecutionStep[PartitionT]):
    """This is an ExecutionStep that can still have functions added to its function pipeline."""

    def __copy__(self) -> OpenExecutionQueue[PartitionT]:
        return OpenExecutionQueue[PartitionT](
            inputs=self.inputs.copy(),
            instructions=self.instructions.copy(),
        )

    def add_instruction(self, instruction: Instruction) -> OpenExecutionQueue[PartitionT]:
        """Append an instruction to this ExecutionStep's pipeline."""
        self.instructions.append(instruction)
        return self

    def as_materialization_request(self) -> MaterializationRequest[PartitionT]:
        """Create an MaterializationRequest from this ExecutionStep.

        Returns a "frozen" version of this ExecutionStep that cannot have instructions added.
        """
        return MaterializationRequest[PartitionT](
            inputs=self.inputs,
            instructions=self.instructions,
            num_results=1,
        )

    def as_materialization_request_multi(self, num_results: int) -> MaterializationRequestMulti[PartitionT]:
        """Create an MaterializationRequestMulti from this ExecutionStep.

        Same as as_materization_request, except the output of this ExecutionStep is a list of partitions.
        This is intended for execution steps that do a fanout.
        """
        return MaterializationRequestMulti[PartitionT](
            inputs=self.inputs,
            instructions=self.instructions,
            num_results=num_results,
        )


@dataclass
class MaterializationRequestBase(ExecutionStep[PartitionT]):
    """Common helpers for MaterializationRequest and MaterializationRequestMulti.
    See those classes for more details.

    num_results: The number of partitions that will be returned.
    _id: A unique identifier for this ExecutionStep.
    """

    num_results: int
    _id: int = field(default_factory=lambda: next(ID_GEN))

    def id(self) -> str:
        return f"{self.__class__.__name__}_{self._id}"

    def __str__(self) -> str:
        return (
            f"{self.id()}\n"
            f"  Inputs: {self.inputs}\n"
            f"  Instructions: {[i.__class__.__name__ for i in self.instructions]}"
        )


@dataclass
class MaterializationRequest(MaterializationRequestBase[PartitionT]):
    """An ExecutionStep that is ready to run. More instructions cannot be added.

    result: When available, the partition created from run the ExecutionStep.
    """

    result: None | MaterializationResult[PartitionT] = None


@dataclass
class MaterializationRequestMulti(MaterializationRequestBase[PartitionT]):
    """An ExecutionStep that is ready to run. More instructions cannot be added.
    This ExecutionStep will return a list of any number of partitions.

    results: When available, the partitions created from run the ExecutionStep.
    """

    results: None | list[MaterializationResult[PartitionT]] = None


class MaterializationResult(Protocol[PartitionT]):
    """A protocol for accessing the result partition of a ExecutionStep.

    Different Runners can fill in their own implementation here.
    """

    def partition(self) -> PartitionT:
        """Get the partition of this result."""
        ...

    def metadata(self) -> PartitionMetadata:
        """Get the metadata of the partition in this result."""
        ...

    def cancel(self) -> None:
        """If possible, cancel execution of this ExecutionStep."""
        ...

    def _noop(self, _: PartitionT) -> None:
        """Implement this as a no-op.
        https://peps.python.org/pep-0544/#overriding-inferred-variance-of-protocol-classes
        """
        ...


class Instruction(Protocol):
    """An instruction is a function to run over a list of partitions.

    Most instructions take one partition and return another partition.
    However, some instructions take one partition and return many partitions (fanouts),
    and others take many partitions and return one partition (reduces).
    To accomodate these, instructions are typed as list[vPartition] -> list[vPartition].
    """

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        # (Dispatching a descriptively named helper here will aid profiling.)
        ...


@dataclass(frozen=True)
class ReadFile(Instruction):
    partition_id: int
    logplan: logical_plan.TabularFilesScan

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._read_file(inputs)

    def _read_file(self, inputs: list[vPartition]) -> list[vPartition]:
        assert len(inputs) == 1
        [filepaths_partition] = inputs
        partition = LocalLogicalPartitionOpRunner()._handle_tabular_files_scan(
            inputs={self.logplan._filepaths_child.id(): filepaths_partition},
            scan=self.logplan,
            partition_id=self.partition_id,
        )
        return [partition]


@dataclass(frozen=True)
class WriteFile(Instruction):
    partition_id: int
    logplan: logical_plan.FileWrite

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._write_file(inputs)

    def _write_file(self, inputs: list[vPartition]) -> list[vPartition]:
        [input] = inputs
        partition = LocalLogicalPartitionOpRunner()._handle_file_write(
            inputs={self.logplan._children()[0].id(): input},
            file_write=self.logplan,
            partition_id=self.partition_id,
        )
        return [partition]


@dataclass(frozen=True)
class Filter(Instruction):
    predicate: ExpressionList

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._filter(inputs)

    def _filter(self, inputs: list[vPartition]) -> list[vPartition]:
        [input] = inputs
        return [input.filter(self.predicate)]


@dataclass(frozen=True)
class Project(Instruction):
    projection: ExpressionList

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._project(inputs)

    def _project(self, inputs: list[vPartition]) -> list[vPartition]:
        [input] = inputs
        return [input.eval_expression_list(self.projection)]


@dataclass(frozen=True)
class LocalLimit(Instruction):
    limit: int

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._limit(inputs)

    def _limit(self, inputs: list[vPartition]) -> list[vPartition]:
        [input] = inputs
        return [input.head(self.limit)]


@dataclass(frozen=True)
class MapPartition(Instruction):
    map_op: MapPartitionOp

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._map_partition(inputs)

    def _map_partition(self, inputs: list[vPartition]) -> list[vPartition]:
        [input] = inputs
        return [self.map_op.run(input)]


@dataclass(frozen=True)
class Sample(Instruction):
    sort_by: ExpressionList
    num_samples: int = 20

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._sample(inputs)

    def _sample(self, inputs: list[vPartition]) -> list[vPartition]:
        [input] = inputs
        result = (
            input.sample(self.num_samples)
            .eval_expression_list(self.sort_by)
            .filter(ExpressionList([~e.to_column_expression().is_null() for e in self.sort_by]))
        )
        return [result]


@dataclass(frozen=True)
class Aggregate(Instruction):
    to_agg: list[tuple[Expression, str]]
    group_by: ExpressionList | None

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._aggregate(inputs)

    def _aggregate(self, inputs: list[vPartition]) -> list[vPartition]:
        [input] = inputs
        return [input.agg(self.to_agg, self.group_by)]


@dataclass(frozen=True)
class Join(Instruction):
    logplan: logical_plan.Join

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._join(inputs)

    def _join(self, inputs: list[vPartition]) -> list[vPartition]:
        [left, right] = inputs
        result = left.join(
            right,
            left_on=self.logplan._left_on,
            right_on=self.logplan._right_on,
            output_projection=self.logplan._output_projection,
            how=self.logplan._how.value,
        )
        return [result]


class ReduceInstruction(Instruction):
    ...


@dataclass(frozen=True)
class ReduceMerge(ReduceInstruction):
    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._reduce_merge(inputs)

    def _reduce_merge(self, inputs: list[vPartition]) -> list[vPartition]:
        return [vPartition.merge_partitions(inputs, verify_partition_id=False)]


@dataclass(frozen=True)
class ReduceMergeAndSort(ReduceInstruction):
    sort_by: ExpressionList
    descending: list[bool]

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._reduce_merge_and_sort(inputs)

    def _reduce_merge_and_sort(self, inputs: list[vPartition]) -> list[vPartition]:
        partition = SortOp.reduce_fn(
            mapped_outputs=inputs,
            exprs=self.sort_by,
            descending=self.descending,
        )
        return [partition]


@dataclass(frozen=True)
class ReduceToQuantiles(ReduceInstruction):
    num_quantiles: int
    sort_by: ExpressionList
    descending: list[bool]

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._reduce_to_quantiles(inputs)

    def _reduce_to_quantiles(self, inputs: list[vPartition]) -> list[vPartition]:
        merged = vPartition.merge_partitions(inputs, verify_partition_id=False)

        # Skip evaluation of expressions by converting to Column Expression, since evaluation was done in Sample
        merged_sorted = merged.sort(self.sort_by.to_column_expressions(), descending=self.descending)

        result = merged_sorted.quantiles(self.num_quantiles)
        return [result]


@dataclass(frozen=True)
class FanoutInstruction(Instruction):
    num_outputs: int


@dataclass(frozen=True)
class FanoutRandom(FanoutInstruction):
    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._fanout_random(inputs)

    def _fanout_random(self, inputs: list[vPartition]) -> list[vPartition]:
        [input] = inputs
        partitions_with_ids = RepartitionRandomOp.map_fn(
            input=input,
            output_partitions=self.num_outputs,
        )
        return [partition for _, partition in sorted(partitions_with_ids.items())]


@dataclass(frozen=True)
class FanoutHash(FanoutInstruction):
    partition_by: ExpressionList

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._fanout_hash(inputs)

    def _fanout_hash(self, inputs: list[vPartition]) -> list[vPartition]:
        [input] = inputs
        partitions_with_ids = RepartitionHashOp.map_fn(
            input=input,
            output_partitions=self.num_outputs,
            exprs=self.partition_by,
        )
        return [partition for i, partition in sorted(partitions_with_ids.items())]


@dataclass(frozen=True)
class FanoutRange(FanoutInstruction, Generic[PartitionT]):
    sort_by: ExpressionList
    descending: list[bool]

    def run(self, inputs: list[vPartition]) -> list[vPartition]:
        return self._fanout_range(inputs)

    def _fanout_range(self, inputs: list[vPartition]) -> list[vPartition]:
        [boundaries, input] = inputs

        partitions_with_ids = SortOp.map_fn(
            input=input,
            output_partitions=self.num_outputs,
            exprs=self.sort_by,
            boundaries=boundaries,
            descending=self.descending,
        )
        return [partition for _, partition in sorted(partitions_with_ids.items())]
