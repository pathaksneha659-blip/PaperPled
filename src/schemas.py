from datetime import datetime
from enum import Enum
from typing import Literal, Optional, Any
from pydantic import BaseModel, HttpUrl, Field

class SourceInfo(BaseModel):
    name: str
    url: str

class PricingModelEnum(str, Enum):
    FREE = "FREE"
    FREEMIUM = "FREEMIUM"
    PAID = "PAID"
    ENTERPRISE = "ENTERPRISE"

# 1. Startup Entity
class StartupData(BaseModel):
    employeeCount: Optional[int] = None
    website: Optional[str] = None
    batch: Optional[str] = None
    tags: list[str] = Field(default_factory=list)

class StartupContent(BaseModel):
    entityName: str
    data: StartupData = Field(default_factory=StartupData)

class StartupRecord(BaseModel):
    schemaVersion: str = "1.0"
    recordType: Literal["STARTUP"] = "STARTUP"
    source: SourceInfo
    content: StartupContent
    collectedAt: str

# 2. Product Entity
class ProductContent(BaseModel):
    productName: str
    startupName: str
    pricingModel: PricingModelEnum

class ProductRecord(BaseModel):
    schemaVersion: str = "1.0"
    recordType: Literal["PRODUCT"] = "PRODUCT"
    source: SourceInfo
    content: ProductContent
    collectedAt: str

# 3. Research Paper Entity
class ResearchPaperContent(BaseModel):
    title: str
    authors: list[str]
    paper_url: str
    github_url: Optional[str] = None
    github_stars: Optional[int] = None
    published_date: str

class ResearchPaperRecord(BaseModel):
    schemaVersion: str = "1.0"
    recordType: Literal["RESEARCH_PAPER"] = "RESEARCH_PAPER"
    source: SourceInfo
    content: ResearchPaperContent
    collectedAt: str