"""User management API routes."""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.session import get_db
from database.models import User, Order
from backend.schemas import UserUpsert, UserResponse


router = APIRouter()


@router.post("/", response_model=UserResponse)
async def upsert_user(
    data: UserUpsert,
    db: AsyncSession = Depends(get_db),
):
    """Create or update user and assign role."""
    stmt = select(User).where(User.telegram_id == data.telegram_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if data.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="Invalid role")

    if user:
        # Обновляем только непустые поля и роль.
        if data.username is not None:
            user.username = data.username
        if data.full_name is not None:
            user.full_name = data.full_name
        user.role = data.role
    else:
        user = User(
            telegram_id=data.telegram_id,
            username=data.username,
            full_name=data.full_name,
            role=data.role,
        )
        db.add(user)
        await db.flush()

    await db.flush()
    await db.refresh(user)
    # Обновить username ответственного во всех заявках, чтобы в «История заявок» отображался @username, а не id
    if data.username is not None and data.telegram_id:
        await db.execute(
            update(Order)
            .where(Order.responsible_telegram_id == data.telegram_id)
            .values(responsible_username=data.username)
        )
        await db.flush()
    return UserResponse.model_validate(user)


@router.get("/", response_model=list[UserResponse])
async def list_users(
    role: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    """List users, optionally filtered by role (e.g. role=admin)."""
    stmt = select(User)
    if role is not None:
        stmt = stmt.where(User.role == role)
    result = await db.execute(stmt)
    users = result.scalars().all()
    return [UserResponse.model_validate(u) for u in users]


@router.get("/{telegram_id}", response_model=UserResponse)
async def get_user(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get user by telegram_id."""
    stmt = select(User).where(User.telegram_id == telegram_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return UserResponse.model_validate(user)


@router.delete("/{telegram_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Disable user by telegram_id (soft delete).

    Нельзя физически удалять пользователя, т.к. на него ссылаются заявки (orders.author_id NOT NULL).
    Вместо удаления переводим пользователя в роль, которая не имеет доступа к боту.
    """
    stmt = select(User).where(User.telegram_id == telegram_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.role = "blocked"
    user.username = None
    user.full_name = None
    await db.flush()
    return None

