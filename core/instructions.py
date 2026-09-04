# Инструкции по подключению — правьте только этот файл, код бота трогать не нужно.

APP_LINK_ANDROID = "https://play.google.com/store/apps/details?id=com.adguard.trusttunnel"
APP_LINK_IOS = "https://apps.apple.com/us/app/trusttunnel/id6755807890?l=ru"

FALLBACK_LINK_NOTE = (
    "вашей ссылке для подключения (если под рукой нет — нажмите «🔗 Моя ссылка» в меню)"
)


def render_android_instructions(link: str = None) -> str:
    """
    Interactive (in-bot) version. If `link` is given, it's embedded directly
    at the "open your link" step — used right after trial registration or
    after "Моя ссылка", где карточка со ссылкой may not be repeated
    in the same message.
    """
    link_line = link if link else FALLBACK_LINK_NOTE

    return (
        "🤖 Подключение на Android\n\n"
        "1. Установите приложение TrustTunnel:\n"
        f"{APP_LINK_ANDROID}\n\n"
        f"2. Перейдите по вашей ссылке для подключения: {link_line}\n\n"
        "3. Нажмите синюю кнопку \"Open in TrustTunnel App\" — настройки сервера "
        "подтянутся в приложение автоматически.\n\n"
        "4. Когда откроется приложение, пролистайте немного вниз и нажмите кнопку "
        "\"Add\"\n\n"
        "5. Затем нажмите на круглую кнопку переключатель рядом с сервером "
        "(синий — включено).\n\n"
        "Если что-то не получается — просто напишите мне (кнопка \"✉️ Написать "
        "администратору\"), помогу разобраться. 🙌"
    )


def render_ios_instructions(link: str = None) -> str:
    link_line = link if link else FALLBACK_LINK_NOTE

    return (
        "📱 Подключение на iPhone\n\n"
        "В российском App Store приложения TrustTunnel нет, поэтому сначала нужно "
        "на минуту сменить регион магазина (это бесплатно, приложение останется "
        "бесплатным):\n\n"
        "1. Откройте App Store → нажмите на аватар профиля (справа вверху) → "
        "ваш профиль → \"Регион\"\n"
        "2. Выберите \"Казахстан\"\n"
        "3. При заполнении платёжных данных:\n"
        "   • Способ оплаты — None (без карты)\n"
        "   • Улица (Street) — Nursultan\n"
        "   • Область (Region) — Almaty\n"
        "   • Телефон (Phone) — 999 999 99 99\n"
        "4. Нажмите \"Готово\"\n\n"
        "Теперь приложение доступно:\n\n"
        "5. Установите TrustTunnel:\n"
        f"{APP_LINK_IOS}\n\n"
        f"6. Перейдите по вашей ссылке для подключения: {link_line}\n\n"
        "7. Нажмите синюю кнопку \"Open in TrustTunnel App\" — настройки сервера "
        "подтянутся в приложение автоматически.\n\n"
        "8. Когда откроется приложение, пролистайте немного вниз и нажмите кнопку "
        "\"Add\"\n\n"
        "9. Затем нажмите на круглую кнопку переключатель рядом с сервером "
        "(синий — включено).\n\n"
        "Если что-то не получается — просто напишите мне (кнопка \"✉️ Написать "
        "администратору\"), помогу разобраться. 🙌"
    )


# Используется в РУЧНОМ режиме (когда админ сам копирует и отправляет карточку
# новому клиенту — часто ДО того, как у клиента появится доступ к Telegram,
# например через WhatsApp). Установку приложения админ в этом режиме уже
# объясняет отдельно сам, до отправки карточки — поэтому здесь только шаги
# ПОСЛЕ установки, без объяснения про App Store/Google Play. Android и iOS
# на этом этапе делают одно и то же, поэтому блок один общий.
MANUAL_CONNECT_STEPS = (
    "📲 Как подключиться (после установки приложения):\n\n"
    "1. Откройте ссылку из карточки выше\n\n"
    "2. Нажмите синюю кнопку \"Open in TrustTunnel App\" — настройки сервера "
    "подтянутся в приложение автоматически.\n\n"
    "3. Когда откроется приложение, пролистайте немного вниз и нажмите кнопку "
    "\"Add\"\n\n"
    "4. Затем нажмите на круглую кнопку переключатель рядом с сервером "
    "(синий — включено)."
)


# ---------------- ROUTING / BYPASS (RU sites while VPN is on) ----------------
#
# "Bypass" in the app = "не пускать через VPN, а пустить напрямую". Adding
# these domains there is what makes Russian banks/services/marketplaces work
# correctly even while the tunnel is on (otherwise many of them geo-block or
# behave oddly seeing a non-RU IP).

ROUTING_INTRO = (
    "🇷🇺 Чтобы российские сайты и приложения (Госуслуги, банки, VK, Ozon, "
    "Wildberries и т.д.) продолжали нормально работать, даже когда TrustTunnel "
    "включён, нужно один раз добавить их в список Bypass — это значит "
    "«трафик к этим адресам идёт напрямую, минуя VPN».\n\n"
    "1. Откройте приложение TrustTunnel → внизу вкладка \"Routing\"\n"
    "2. Нажмите \"Default profile\"\n"
    "3. Переключитесь на вкладку \"Bypass\" (сверху слева)\n"
    "4. Скопируйте список ниже (кнопка под этим сообщением) и вставьте его "
    "целиком в поле\n"
    "5. Нажмите \"Save\"\n\n"
    "Выберите список под вашу платформу:"
)

ANDROID_BYPASS_DOMAINS = """vk.com
*.vk.com
vk.ru
*.vk.ru
vkontakte.ru
*.vkontakte.ru
vk.me
*.vk.me
id.vk.com
login.vk.com
oauth.vk.com
vkvideo.ru
*.vkvideo.ru
vkuservideo.net
*.vkuservideo.net
*.vkuservideo.com
*.vk-cdn.net
*.vkcache.com
api.vk.com
*.api.vk.com
*.userapi.com
*.vkuseraudio.net
*.vkuseraudio.com
max.ru
*.max.ru
msg.max.ru
api.max.ru
auth.max.ru
*.gosuslugi.ru
*.esia.gosuslugi.ru
*.digital.gov.ru
*.cdn.max.ru
*.static.max.ru
*.media.max.ru
*.ws.max.ru
*.ru
ru.max.messenger
ru.yandex.mail
*.yandex.ru
*.yandex.net
*.yandex.com
com.wildberries.ru
*.wildberries.ru
*.wb.ru
*.wbstatic.net
*.wbbasket.ru
*.content.wb.ru
ru.rostel
ru.dublgis.dgismobile
com.avito.android
com.yandex.aliceapp
ru.alfabank.mobile.android
com.gnivts.selfemployed
ru.rzd.pass
ru.sberbankmobile
com.idamob.tinkoff.android
ru.yandex.music
ru.oneme.app
ru.ozon.app.android
ru.ozon.fintech.finance
ru.vk.store
ru.rutube.app
com.vkontakte.android"""

# NOTE: этот список специально короче/без Google-строк в конце (google.com,
# googleapis.com, gstatic.com, android.clients.google.com,
# play.googleapis.com) — это Android-специфичные системные адреса, на iOS
# им тут не место.
IOS_BYPASS_DOMAINS = """vk.com
vk.ru
vkontakte.ru
vk.me
id.vk.com
login.vk.com
oauth.vk.com
api.vk.com
vkvideo.ru
vkuservideo.net
vkuservideo.com
vk-cdn.net
vkcache.com
userapi.com
vkuseraudio.net
vkuseraudio.com
vk-portal.net
vkuser.net
mail.ru
vkid.ru

max.ru
msg.max.ru
api.max.ru
auth.max.ru
cdn.max.ru
static.max.ru
media.max.ru
ws.max.ru

gosuslugi.ru
esia.gosuslugi.ru
digital.gov.ru
pos.gosuslugi.ru

yandex.ru
yandex.net
yandex.com
ya.ru
ru.yandex.mail
yastatic.net
yandexstatic.net

wildberries.ru
wb.ru
wbstatic.net
wbbasket.ru
content.wb.ru
cdn.wildberries.ru
api.wildberries.ru
seller.wildberries.ru
security.wildberries.ru
chat.wildberries.ru
video.wildberries.ru
finance.wildberries.ru
static.wildberries.ru
images.wildberries.ru
wbpay.ru
wbank.ru
wildberries-bank.ru

ozon.ru
ozon.com
finance.ozon.ru
seller.ozon.ru
api.ozon.ru
cdn.ozon.ru
ir.ozon.ru
i.ozon.ru
s.ozon.ru
pay.ozon.ru
checkout.ozon.ru
o3t.ru
o3.ru
ozon-dostavka.ru

rutube.ru
rutube.app"""
