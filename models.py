from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from typing_extensions import Annotated

Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=40, pattern=r"^[a-zA-Z0-9_.-]+$")]
Password = Annotated[str, StringConstraints(min_length=8, max_length=128)]
Budget = Annotated[float, Field(gt=0, le=100_000_000)]
MonthlyAmount = Annotated[float, Field(ge=0, le=100_000_000)]
Count = Annotated[int, Field(ge=0, le=50)]


class RegisterInput(BaseModel):
    username: Username
    email: EmailStr
    full_name: str = Field(default="", max_length=100)
    password: Password


class HomeBudgetInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_budget: Budget
    number_of_lights: Count = 0
    ceiling_fans: Count = 0
    furniture_pieces: Count = 0
    dining_tables: Count = 0
    rooms: list[str] = Field(default_factory=list, max_length=3)
    additional_requirements: str = Field(default="", max_length=2000)


class PartyBudgetInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_budget: Budget
    guest_count: Annotated[int, Field(ge=1, le=100_000)]
    party_type: str = Field(min_length=2, max_length=60)
    venue_type: str = Field(default="", max_length=100)
    needs_catering: bool = True
    needs_decoration: bool = True
    needs_entertainment: bool = False
    additional_requirements: str = Field(default="", max_length=2000)


class JewelryBudgetInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_budget: Budget
    occasion: str = Field(min_length=2, max_length=100)
    preferences: str = Field(default="", max_length=2000)


class SessionDataInput(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)


class FinancialProfileInput(BaseModel):
    monthly_net_income: Annotated[float, Field(gt=0, le=100_000_000)]
    monthly_essential_expenses: MonthlyAmount
    monthly_savings_goal: MonthlyAmount
