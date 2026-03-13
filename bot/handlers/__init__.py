from aiogram import Router
from bot.handlers import (
    main_menu,
    create_order,
    my_orders,
    history,
    yandex_link,
    template_order,
    users,
    fallback,
)

router = Router()
router.include_router(main_menu.router)
router.include_router(create_order.router)
router.include_router(template_order.router)
router.include_router(my_orders.router)
router.include_router(history.router)
router.include_router(yandex_link.router)
router.include_router(users.router)
router.include_router(fallback.router)  # последним — ловит всё необработанное
