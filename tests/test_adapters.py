import base64
import hashlib
import hmac
import unittest

from paidanswer.channels import TelegramChannel, WhatsAppChannel, parse_telegram_update, parse_whatsapp_payload
from paidanswer.llm import AllProvidersFailed, AnthropicLLM, GeminiLLM, LLMChain
from paidanswer.payments import RazorpayPayments, verify_hmac_sha256
from paidanswer.speech import GeminiSTT, GoogleTTS
from paidanswer.testing import FakeHTTP, FakeLLM, FakeResponse


class SignatureTests(unittest.TestCase):
    def test_hmac(self):
        body, secret = b'{"a":1}', "s3cret"
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_hmac_sha256(body, sig, secret))
        self.assertTrue(verify_hmac_sha256(body, "sha256=" + sig, secret, prefix="sha256="))
        self.assertFalse(verify_hmac_sha256(body, sig, "other"))
        self.assertFalse(verify_hmac_sha256(body + b" ", sig, secret))
        self.assertFalse(verify_hmac_sha256(body, None, secret))
        self.assertFalse(verify_hmac_sha256(body, sig, ""))
        self.assertFalse(verify_hmac_sha256(body, sig, secret, prefix="sha256="))


class ParserTests(unittest.TestCase):
    def test_telegram(self):
        m = parse_telegram_update({"update_id": 5, "message": {"chat": {"id": 77}, "text": "hi"}})
        self.assertEqual((m.channel, m.chat_id, m.text, m.message_id), ("telegram", "77", "hi", "5"))
        v = parse_telegram_update({"update_id": 6, "message": {"chat": {"id": 77}, "voice": {"file_id": "F", "mime_type": "audio/ogg"}}})
        self.assertEqual((v.media_ref, v.media_mime), ("F", "audio/ogg"))
        self.assertIsNone(parse_telegram_update({"update_id": 7, "edited_channel_post": {}}))

    def test_whatsapp(self):
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "9198", "id": "w1", "type": "text", "text": {"body": "hello"}},
            {"from": "9198", "id": "w2", "type": "audio", "audio": {"id": "M1", "mime_type": "audio/ogg; codecs=opus"}},
            {"from": "9198", "id": "w3", "type": "image"}]}}]}]}
        msgs = parse_whatsapp_payload(payload)
        self.assertEqual([m.message_id for m in msgs], ["w1", "w2"])
        self.assertEqual(msgs[1].media_ref, "M1")
        self.assertEqual(parse_whatsapp_payload({"entry": [{"changes": [{"value": {"statuses": [{}]}}]}]}), [])


class ChannelRequestTests(unittest.TestCase):
    def test_telegram_requests(self):
        http = FakeHTTP(FakeResponse(200, {"ok": True}), FakeResponse(200, {"ok": True}),
                        FakeResponse(200, {"ok": True, "result": {"file_path": "voice/a.oga"}}), FakeResponse(200, content=b"AUDIO"))
        ch = TelegramChannel("TOKEN", http=http)
        ch.send_text("1", "hello")
        ch.send_voice("1", b"ogg")
        data, mime = ch.download_media("FILE")
        self.assertEqual(http.calls[0][1], "https://api.telegram.org/botTOKEN/sendMessage")
        self.assertEqual(http.calls[0][2]["json"], {"chat_id": "1", "text": "hello"})
        self.assertEqual(http.calls[1][1], "https://api.telegram.org/botTOKEN/sendVoice")
        self.assertIn("voice", http.calls[1][2]["files"])
        self.assertEqual(http.calls[3][1], "https://api.telegram.org/file/botTOKEN/voice/a.oga")
        self.assertEqual((data, mime), (b"AUDIO", "audio/ogg"))

    def test_telegram_error_raises(self):
        ch = TelegramChannel("T", http=FakeHTTP(FakeResponse(403, {"ok": False, "description": "blocked"})))
        with self.assertRaises(RuntimeError):
            ch.send_text("1", "x")

    def test_whatsapp_requests(self):
        http = FakeHTTP(FakeResponse(200, {}), FakeResponse(200, {"id": "MEDIA9"}), FakeResponse(200, {}),
                        FakeResponse(200, {"url": "https://cdn/x", "mime_type": "audio/ogg"}), FakeResponse(200, content=b"A"))
        ch = WhatsAppChannel("TOK", "PNID", "v99.0", http=http)
        ch.send_text("9198", "hi")
        ch.send_voice("9198", b"ogg")
        audio, mime = ch.download_media("M1")
        self.assertEqual(http.calls[0][1], "https://graph.facebook.com/v99.0/PNID/messages")
        self.assertEqual(http.calls[0][2]["headers"], {"Authorization": "Bearer TOK"})
        self.assertEqual(http.calls[0][2]["json"]["text"], {"body": "hi"})
        self.assertEqual(http.calls[1][1], "https://graph.facebook.com/v99.0/PNID/media")
        self.assertEqual(http.calls[2][2]["json"]["audio"], {"id": "MEDIA9"})
        self.assertEqual(http.calls[3][1], "https://graph.facebook.com/v99.0/M1")
        self.assertEqual((audio, mime), (b"A", "audio/ogg"))

    def test_whatsapp_error_raises(self):
        ch = WhatsAppChannel("T", "P", "v1", http=FakeHTTP(FakeResponse(400, {})))
        with self.assertRaises(RuntimeError):
            ch.send_text("1", "x")


class RazorpayTests(unittest.TestCase):
    def test_create_link(self):
        http = FakeHTTP(FakeResponse(200, {"id": "plink_X", "short_url": "https://rzp.io/i/abc"}))
        link = RazorpayPayments("kid", "ksec", http=http).create_link("ord123", 9900, "INR", "desc")
        method, url, kw = http.calls[0]
        self.assertEqual((link.id, link.url), ("plink_X", "https://rzp.io/i/abc"))
        self.assertEqual(url, "https://api.razorpay.com/v1/payment_links")
        self.assertEqual(kw["auth"], ("kid", "ksec"))
        self.assertEqual((kw["json"]["amount"], kw["json"]["currency"], kw["json"]["reference_id"]), (9900, "INR", "ord123"))

    def test_refund_once_then_skip(self):
        http = FakeHTTP(FakeResponse(200, {"amount": 9900, "amount_refunded": 0}), FakeResponse(200, {"id": "rfnd_1"}),
                        FakeResponse(200, {"amount": 9900, "amount_refunded": 9900}))
        rz = RazorpayPayments("k", "s", http=http)
        self.assertEqual(rz.refund("pay_1", 9900, "ord"), "rfnd_1")
        self.assertEqual(http.calls[1][1], "https://api.razorpay.com/v1/payments/pay_1/refund")
        self.assertEqual(http.calls[1][2]["json"]["amount"], 9900)
        self.assertEqual(rz.refund("pay_1", 9900, "ord"), "already_refunded")
        self.assertEqual(len(http.calls), 3)   # the second call did not POST a refund


class ModelRequestTests(unittest.TestCase):
    def test_gemini(self):
        http = FakeHTTP(FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "Hello "}, {"text": "world"}]}}]}))
        out = GeminiLLM("KEY", "gem-model", http=http).answer("Q?", "SYS")
        method, url, kw = http.calls[0]
        self.assertEqual(out, "Hello world")
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1beta/models/gem-model:generateContent")
        self.assertEqual(kw["params"], {"key": "KEY"})
        self.assertEqual(kw["json"]["systemInstruction"]["parts"][0]["text"], "SYS")
        self.assertEqual(kw["json"]["contents"][0]["parts"][0]["text"], "Q?")

    def test_gemini_empty_is_an_error(self):
        http = FakeHTTP(FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": " "}]}}]}))
        with self.assertRaises(RuntimeError):
            GeminiLLM("K", "m", http=http).answer("Q", "S")

    def test_anthropic(self):
        http = FakeHTTP(FakeResponse(200, {"content": [{"type": "text", "text": "Hi there"}]}))
        out = AnthropicLLM("AKEY", "claude-x", http=http).answer("Q", "SYS")
        method, url, kw = http.calls[0]
        self.assertEqual(out, "Hi there")
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(kw["headers"]["x-api-key"], "AKEY")
        self.assertEqual(kw["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual((kw["json"]["system"], kw["json"]["model"]), ("SYS", "claude-x"))

    def test_tts_and_stt(self):
        audio = base64.b64encode(b"OGGDATA").decode()
        tts_http = FakeHTTP(FakeResponse(200, {"audioContent": audio}))
        out, mime = GoogleTTS("K", http=tts_http).synthesize("hello")
        self.assertEqual((out, mime), (b"OGGDATA", "audio/ogg"))
        self.assertEqual(tts_http.calls[0][2]["json"]["audioConfig"], {"audioEncoding": "OGG_OPUS"})
        stt_http = FakeHTTP(FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": " transcript "}]}}]}))
        text = GeminiSTT("K", "m", http=stt_http).transcribe(b"AUDIO", "audio/ogg")
        part = stt_http.calls[0][2]["json"]["contents"][0]["parts"][0]["inline_data"]
        self.assertEqual(text, "transcript")
        self.assertEqual((part["mime_type"], base64.b64decode(part["data"])), ("audio/ogg", b"AUDIO"))

    def test_chain_falls_back_and_reports_all_errors(self):
        self.assertEqual(LLMChain([FakeLLM(default="fail"), FakeLLM()]).answer("q", "s"), "The answer is 42.")
        with self.assertRaises(AllProvidersFailed) as ctx:
            LLMChain([FakeLLM(default="fail", name="a"), FakeLLM(default="fail", name="b")]).answer("q", "s")
        self.assertIn("a:", str(ctx.exception))
        self.assertIn("b:", str(ctx.exception))
        with self.assertRaises(ValueError):
            LLMChain([])


if __name__ == "__main__":
    unittest.main()
