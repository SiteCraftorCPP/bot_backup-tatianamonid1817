"""Keyboards for Telegram bot."""
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton


def main_menu_kb(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Main menu keyboard."""
    if is_admin:
        # Для админов: без раздела помощи, расширенное меню.
        buttons = [
            [KeyboardButton(text="📄 Заявка по шаблону")],
            [KeyboardButton(text="📦 Мои заявки")],
            [KeyboardButton(text="🔗 Добавить ссылку на файлы")],
            [KeyboardButton(text="📜 История заявок")],
            [KeyboardButton(text="📊 Статистика")],
        ]
    else:
        # Для обычного пользователя — заявки + помощь.
        buttons = [
            [KeyboardButton(text="📄 Заявка по шаблону")],
            [KeyboardButton(text="📦 Мои заявки")],
            [KeyboardButton(text="❓ Помощь")],
        ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def skip_inline_kb(callback_data: str) -> InlineKeyboardMarkup:
    """Inline button to skip optional field (MS number, comment)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data=callback_data)],
    ])


def back_kb() -> ReplyKeyboardMarkup:
    """Back button."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="« Назад")]],
        resize_keyboard=True,
    )


def attach_file_kb() -> ReplyKeyboardMarkup:
    """Прикрепить файл / Пропустить (ожидание заполненного шаблона)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📎 Прикрепить файл"), KeyboardButton(text="⏭ Пропустить")],
            [KeyboardButton(text="« Назад")],
        ],
        resize_keyboard=True,
    )


def done_extra_kb() -> ReplyKeyboardMarkup:
    """После создания заявки: прикрепить доп. файл или завершить."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📎 Прикрепить доп. файл"), KeyboardButton(text="✅ Готово")],
            [KeyboardButton(text="« Назад")],
        ],
        resize_keyboard=True,
    )


def category_kb() -> ReplyKeyboardMarkup:
    """Category selection: Одежда / Обувь."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Одежда"), KeyboardButton(text="Обувь")],
            [KeyboardButton(text="« Назад")],
        ],
        resize_keyboard=True,
    )


def product_choice_kb() -> ReplyKeyboardMarkup:
    """Product selection: Повторный / Новый товар."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Повторный товар"), KeyboardButton(text="Новый товар")],
            [KeyboardButton(text="« Назад")],
        ],
        resize_keyboard=True,
    )


def add_more_kb() -> ReplyKeyboardMarkup:
    """After adding item: add more or finish."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить ещё"), KeyboardButton(text="✅ Завершить")],
        ],
        resize_keyboard=True,
    )


def legal_entity_kb() -> InlineKeyboardMarkup:
    """Juridical entity selection."""
    entities = [
        "Пашкович", "АКС КЭПИТАЛ", "Банишевский", "Малец",
        "Крикун", "Чайковский", "Чайковская",
    ]
    buttons = [
        [InlineKeyboardButton(text=e, callback_data=f"le:{e}")] for e in entities
    ]
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def country_kb() -> ReplyKeyboardMarkup:
    """Страна производства: КНР, Киргизия, Россия."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="КНР"), KeyboardButton(text="Киргизия"), KeyboardButton(text="Россия")],
            [KeyboardButton(text="« Назад")],
        ],
        resize_keyboard=True,
    )


def target_gender_kb() -> ReplyKeyboardMarkup:
    """Целевой пол: Мужской, Женский, Универсальный."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Мужской"),
                KeyboardButton(text="Женский"),
            ],
            [KeyboardButton(text="Универсальный")],
            [KeyboardButton(text="« Назад")],
        ],
        resize_keyboard=True,
    )


def brands_kb(brands: list[str]) -> ReplyKeyboardMarkup:
    """Brand selection keyboard (for new products)."""
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for b in brands:
        row.append(KeyboardButton(text=b))
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([KeyboardButton(text="« Назад")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def order_type_kb() -> ReplyKeyboardMarkup:
    """Order type: Ламода, ОЗ/ВБ, Киргизия."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Ламода"), KeyboardButton(text="ОЗ/ВБ"), KeyboardButton(text="Киргизия")],
            [KeyboardButton(text="« Назад")],
        ],
        resize_keyboard=True,
    )


def sizes_kb(sizes: list[str]) -> ReplyKeyboardMarkup:
    """Size selection from list."""
    row = []
    kb = []
    for s in sizes[:12]:  # Max 12 sizes
        row.append(KeyboardButton(text=s))
        if len(row) >= 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([KeyboardButton(text="« Назад")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def confirm_kb() -> ReplyKeyboardMarkup:
    """Confirm / Cancel."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Сформировать заявку"), KeyboardButton(text="❌ Отмена")],
            [KeyboardButton(text="« Назад")],
        ],
        resize_keyboard=True,
    )


def orders_list_inline(
    orders: list[tuple[int, str, str]],
    page: int = 0,
    has_next: bool = False,
    prefix: str = "ord",
    show_filters: bool = False,
    current_filter: str | None = None,
    filter_mode: str = "history",
    admin_labels: list[str] | None = None,
) -> InlineKeyboardMarkup:
    """Inline list of orders for selection.
    filter_mode:
      - history: админ, история заявок — все 5 кнопок (все, создана, в работе, готово, отправлена)
      - my_user: пользователь, мои заявки — все, создана, в работе, готова (готова=отправлена)
      - my_admin: админ, мои заявки — в работе, готово
    """
    per_page = 8
    start = page * per_page
    items = orders[start : start + per_page]
    buttons = []

    if show_filters:
        if filter_mode == "history":
            filter_btns = [
                ("Все", "all"),
                ("Создана", "создана"),
                ("В работе", "в работе"),
                ("Готово", "готово"),
                ("Отправлена", "отправлена"),
            ]
        elif filter_mode == "my_user":
            filter_btns = [
                ("Все", "all"),
                ("Создана", "создана"),
                ("В работе", "в работе"),
                ("Готова", "отправлена"),  # готова для юзера = статус отправлена
            ]
        else:  # my_admin
            filter_btns = [
                ("В работе", "в работе"),
                ("Готово", "готово"),
            ]
        row = []
        for label, key in filter_btns:
            suffix = " ✓" if current_filter == key else ""
            row.append(InlineKeyboardButton(text=label + suffix, callback_data=f"flt:{key}"))
            if len(row) >= 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    # Ряд(ы) с именами админов над списком заявок (используется в истории заявок).
    if admin_labels:
        row: list[InlineKeyboardButton] = []
        for label in admin_labels:
            row.append(InlineKeyboardButton(text=label, callback_data="admnoop"))
            if len(row) >= 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        # Визуальный отступ между списком админов и заявками.
        buttons.append([InlineKeyboardButton(text=" ", callback_data="admsep")])

    buttons.extend([
        [InlineKeyboardButton(text=f"№{num} — {status}", callback_data=f"{prefix}:{id_}")]
        for id_, num, status in items
    ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"{prefix}pg:{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"{prefix}pg:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data=f"{prefix}_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# Порядок статусов (для справки). Все 3 админских статуса всегда доступны для корректировки.
STATUS_ORDER = ["создана", "в работе", "готово", "отправлена"]


def order_detail_back_kb(
    is_admin: bool = False,
    order_id: int | None = None,
    current_status: str | None = None,
    can_user_delete: bool = False,
) -> InlineKeyboardMarkup:
    """Back from order detail.

    Если is_admin=True, добавляем:
    - кнопки смены статуса (в работе, готово, отправлена)
    - кнопку «Изменить ответственного».
    """
    buttons = []
    if is_admin and order_id:
        status_btns = [
            ("В работе", "в работе", f"st:in_progress:{order_id}"),
            ("Готово", "готово", f"st:ready:{order_id}"),
            ("Отправлена", "отправлена", f"st:sent:{order_id}"),
        ]
        row = [InlineKeyboardButton(text=label, callback_data=cb) for label, _, cb in status_btns]
        buttons.append(row)
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Изменить ответственного",
                    callback_data=f"change_resp:{order_id}",
                )
            ]
        )
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🗑 Удалить заявку",
                    callback_data=f"adel_confirm:{order_id}",
                )
            ]
        )
    elif (not is_admin) and order_id and can_user_delete:
        # Для пользователя: кнопка удаления заявки
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🗑 Удалить заявку",
                    callback_data=f"del_confirm:{order_id}",
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="orders_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
