#!/usr/bin/env python3
"""Сколько API покрыто — и, что важнее, почему остальное не покрыто.

Голая доля вводит в заблуждение: в схеме лежат секретные чаты, покупки в
магазинах приложений, обработчики на стороне работающего бота и сигнальный
обмен звонков — всё это терминальному клиенту недоступно или не нужно. Поэтому
непокрытое делится на три кучки: сделано через обёртку Telethon, неприменимо,
и настоящий пробел. Считать имеет смысл только последнюю.

Списки отнесений ниже — суждение, а не факт. Их видно, и с ними можно спорить.

    .venv/bin/python tools/coverage.py            # сводка по всем разделам
    .venv/bin/python tools/coverage.py messages   # пробелы одного раздела
"""

import re, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
src = "".join(p.read_text() for p in (ROOT / "bin").glob("*.py"))
direct = set(re.findall(r"functions\.(\w+)\.(\w+)Request", src))
from telethon.tl import functions

# --- правила отнесения -------------------------------------------------------
WRAPPED = {  # tgx делает это, но через высокоуровневый вызов Telethon
 ("messages","DeleteMessages"),("messages","GetHistory"),("messages","GetMessages"),
 ("messages","GetDialogs"),("messages","Search"),("messages","SearchGlobal"),
 ("messages","ReadHistory"),("messages","ReadMessageContents"),("messages","SendMultiMedia"),
 ("messages","GetFullChat"),("messages","GetChats"),("messages","GetPeerDialogs"),
 ("messages","GetPinnedDialogs"),("messages","EditInlineBotMessage"),
 ("channels","DeleteMessages"),("channels","GetChannels"),("channels","GetMessages"),
 ("channels","ReadHistory"),("channels","ReadMessageContents"),
 ("account","InitTakeoutSession"),("account","FinishTakeoutSession"),
 ("contacts","ResolveUsername"),("contacts","ImportContacts"),
}
NA = {  # неприменимо к терминальному клиенту — с причиной
 # секретные чаты живут на устройстве, Telethon их не поддерживает
 ("messages","AcceptEncryption"),("messages","DiscardEncryption"),("messages","GetDhConfig"),
 ("messages","ReadEncryptedHistory"),("messages","ReportEncryptedSpam"),
 ("messages","RequestEncryption"),("messages","SendEncrypted"),("messages","SendEncryptedFile"),
 ("messages","SendEncryptedService"),("messages","SetEncryptedTyping"),
 ("messages","UploadEncryptedFile"),
 # покупки внутри магазинов приложений
 ("payments","AssignAppStoreTransaction"),("payments","AssignPlayMarketTransaction"),
 ("payments","CanPurchaseStore"),
 # обработчики на стороне работающего бота, а не клиента
 ("messages","SetBotCallbackAnswer"),("messages","SetBotPrecheckoutResults"),
 ("messages","SetBotShippingResults"),("messages","SetInlineBotResults"),
 ("messages","SetInlineGameScore"),("messages","SetGameScore"),
 ("messages","SetBotGuestChatResult"),("messages","SendBotRequestedPeer"),
 ("messages","SendWebViewData"),("messages","SendWebViewResultMessage"),
 ("messages","ProlongWebView"),("messages","GetGameHighScores"),
 ("messages","GetInlineGameHighScores"),("messages","SavePreparedInlineMessage"),
 ("messages","GetPreparedInlineMessage"),
 ("bots","AnswerWebhookJSONQuery"),("bots","SendCustomRequest"),
 ("bots","InvokeWebViewCustomMethod"),("bots","SetJoinChatResults"),
 ("bots","GetRequestedWebViewButton"),("bots","RequestWebViewButton"),
 ("bots","CheckDownloadFileParams"),
 # звук и видео звонка идут по WebRTC — из терминала не сыграть
 ("phone","AcceptCall"),("phone","ConfirmCall"),("phone","RequestCall"),
 ("phone","DiscardCall"),("phone","ReceivedCall"),("phone","SendSignalingData"),
 ("phone","SaveCallDebug"),("phone","SaveCallLog"),("phone","SetCallRating"),
 ("phone","GetCallConfig"),("phone","JoinGroupCall"),("phone","LeaveGroupCall"),
 ("phone","JoinGroupCallPresentation"),("phone","LeaveGroupCallPresentation"),
 ("phone","GetGroupCallStreamChannels"),("phone","GetGroupCallChainBlocks"),
 ("phone","SendConferenceCallBroadcast"),("phone","SendGroupCallEncryptedMessage"),
 ("phone","CreateConferenceCall"),("phone","DeclineConferenceCallInvite"),
 ("phone","DeleteConferenceCallParticipants"),("phone","InviteConferenceCallParticipant"),
 # Telegram Passport — отдельная система документов
 ("account","GetAllSecureValues"),("account","GetSecureValue"),("account","SaveSecureValue"),
 ("account","DeleteSecureValue"),("account","GetAuthorizationForm"),
 ("account","AcceptAuthorization"),
 # push-уведомления и состояние устройства
 ("account","RegisterDevice"),("account","UnregisterDevice"),("account","UpdateDeviceLocked"),
 ("account","UpdateStatus"),("account","GetAutoDownloadSettings"),
 ("account","SaveAutoDownloadSettings"),("account","GetWebBrowserSettings"),
 ("account","UpdateWebBrowserSettings"),("account","ToggleWebBrowserSettingsException"),
 ("account","DeleteWebBrowserSettingsExceptions"),
 # смена номера, удаление аккаунта, сброс пароля — необратимое, делается в клиенте
 ("account","ChangePhone"),("account","SendChangePhoneCode"),("account","ConfirmPhone"),
 ("account","SendConfirmPhoneCode"),("account","DeleteAccount"),("account","ResetPassword"),
 ("account","DeclinePasswordReset"),("account","InitPasskeyRegistration"),
 ("account","RegisterPasskey"),("account","DeletePasskey"),("account","GetPasskeys"),
 ("account","VerifyEmail"),("account","VerifyPhone"),("account","SendVerifyEmailCode"),
 ("account","SendVerifyPhoneCode"),("account","CancelPasswordEmail"),
 ("account","ConfirmPasswordEmail"),("account","ResendPasswordEmail"),
 ("account","InvalidateSignInCodes"),
 # служебное
 ("messages","ReceivedMessages"),("messages","ReceivedQueue"),
 ("messages","GetDocumentByHash"),("messages","GetSplitRanges"),
 ("messages","ReportReadMetrics"),("messages","ReportMessagesDelivery"),
 ("messages","ReportMusicListen"),("messages","GetEmojiKeywordsDifference"),
 ("messages","GetEmojiKeywordsLanguages"),("messages","GetEmojiKeywords"),
 ("messages","GetEmojiURL"),
}
DEFAULT = ["payments", "messages", "channels", "account", "phone", "stories",
           "contacts", "bots", "chatlists", "stickers", "stats"]
WANT = sys.argv[1:] or DEFAULT
ONE = len(sys.argv) > 1
print(f"{'раздел':10} {'есть':>6} {'обёртки':>8} {'н/п':>5} {'пробел':>7}  {'по делу':>8}")
print("-"*52)
gaps = {}
T = [0,0,0,0]
for ns in WANT:
    mod = getattr(functions, ns)
    all_m = [n[:-7] for n in dir(mod) if n.endswith("Request") and n != "TLRequest"]
    all_m = [m for m in all_m if m != "TL"]
    have = [m for m in all_m if (ns,m) in direct]
    wrap = [m for m in all_m if (ns,m) not in direct and (ns,m) in WRAPPED]
    na   = [m for m in all_m if (ns,m) not in direct and (ns,m) in NA]
    gap  = [m for m in all_m if (ns,m) not in direct and (ns,m) not in WRAPPED and (ns,m) not in NA]
    gaps[ns] = gap
    reach = len(have)+len(wrap); useful = reach+len(gap)
    T[0]+=len(have); T[1]+=len(wrap); T[2]+=len(na); T[3]+=len(gap)
    print(f"{ns:10} {len(have):6} {len(wrap):8} {len(na):5} {len(gap):7}  {reach/useful*100 if useful else 100:7.0f}%")
print("-"*52)
reach=T[0]+T[1]; useful=reach+T[3]
print(f"{'итого':10} {T[0]:6} {T[1]:8} {T[2]:5} {T[3]:7}  {reach/useful*100:7.0f}%")
print("\n(«по делу» = покрыто / (покрыто + пробелы), без неприменимого)\n")
for ns in WANT:
    if gaps[ns] and (ONE or len(WANT) <= 12):
        print(f"### {ns} — пробелы ({len(gaps[ns])})")
        print(", ".join(sorted(gaps[ns])), "\n")
