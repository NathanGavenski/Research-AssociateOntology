from pydantic import BaseModel
from typing import Literal


class ExtractedEntity(BaseModel):
    term: str
    entity_type: Literal[
        "AISystemCategory", "ProhibitedPractice", "HighRiskArea",
        "Actor", "Authority", "Requirement", "Obligation"
    ]
    label: str    
    definition: str                # one sentence, paraphrased
    article_ref: str               # e.g. "Art. 5(1)(c)"
    source_quote: str              # verbatim span from the chunk
    bearer: str | None = None      # for obligations: which role holds it
    applies_to: str | None = None  # for obligations/requirements: which system class
    confidence: float


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity]
