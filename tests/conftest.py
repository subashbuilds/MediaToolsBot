import importlib.util
import sys
import types

if importlib.util.find_spec('telethon') is None:
    class FakeButton:
        def __init__(self, text, data): self.text, self.data = text, data
        @classmethod
        def inline(cls, text, data): return cls(text, data)

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def add_event_handler(self, *args, **kwargs): pass

    telethon = types.ModuleType('telethon')
    telethon.Button = FakeButton
    telethon.TelegramClient = FakeClient
    telethon.events = types.SimpleNamespace(NewMessage=object, CallbackQuery=object)
    errors = types.ModuleType('telethon.errors')
    errors.MessageNotModifiedError = type('MessageNotModifiedError', (Exception,), {})
    custom_message = types.ModuleType('telethon.tl.custom.message')
    custom_message.Message = object
    tl_custom = types.ModuleType('telethon.tl.custom')
    tl_custom.message = custom_message
    tl = types.ModuleType('telethon.tl')
    tl.custom = tl_custom
    sys.modules.update({
        'telethon': telethon,
        'telethon.errors': errors,
        'telethon.tl': tl,
        'telethon.tl.custom': tl_custom,
        'telethon.tl.custom.message': custom_message,
    })
