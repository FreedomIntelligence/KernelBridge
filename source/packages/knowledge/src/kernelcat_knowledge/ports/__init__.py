"""Ports for KernelCat-Wiki application services."""
from kernelcat_knowledge.ports.errors import (
    ApplicationError,
    ArtifactUnavailableError,
    CorruptRecordError,
    IntegrityError,
    RepositoryError,
)
from kernelcat_knowledge.ports.repositories import (
    ArtifactResolver,
    CardReadRepository,
    IndexReadModel,
    PageReaderPort,
    RawReaderPort,
    ReviewQueueRepository,
    SourceContentReader,
    SourceRepository,
    TrajectoryRepository,
)
from kernelcat_knowledge.ports.wiki import (
    EvidenceRepository,
    KnowledgeRepository,
    ReadModel,
    TrajectoryOutbox,
)

__all__ = [
    "ApplicationError",
    "ArtifactResolver",
    "ArtifactUnavailableError",
    "CardReadRepository",
    "CorruptRecordError",
    "EvidenceRepository",
    "IndexReadModel",
    "IntegrityError",
    "KnowledgeRepository",
    "PageReaderPort",
    "RawReaderPort",
    "ReadModel",
    "RepositoryError",
    "ReviewQueueRepository",
    "SourceContentReader",
    "SourceRepository",
    "TrajectoryOutbox",
    "TrajectoryRepository",
]

# NOTE: ``SourceRepository`` above resolves to ``.repositories`` (the
# repository-style port used by the stage-2 source catalog services). A
# method-style protocol with the same name lives in ``.wiki`` for
# ``WikiApplication``; import it explicitly from that module when needed.
from kernelcat_knowledge.ports.wiki import (
    SourceRepository as WikiSourceRepository,  # noqa: F401
)
