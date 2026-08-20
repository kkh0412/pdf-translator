from __future__ import annotations

import os
import unittest
from unittest import mock

from worker import translator
from worker.google_translate import GoogleTranslateIntegrityError


class TranslationValidationTests(unittest.TestCase):
    def test_inline_math_may_move_to_sentence_boundary(self) -> None:
        source = "For the state [[MATH_0]] the entropy is invariant under this map."
        translated = "이 사상에서 엔트로피는 불변이며 그 값은 [[MATH_0]]."

        # Target-language grammar may legitimately place inline math at the end
        # of a sentence. Exact placeholder preservation is the hard invariant.
        translator._validate_translation_result(source, translated, "ko")

    def test_inline_math_placeholder_mutation_is_rejected(self) -> None:
        source = "For [[MATH_0]] we obtain the following result."
        translated = "[[MATH_1]]에 대해 다음 결과를 얻는다."

        with self.assertRaises(translator.TranslationValidationError):
            translator._validate_translation_result(source, translated, "ko")


class TranslationRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        print_patcher = mock.patch("builtins.print")
        print_patcher.start()
        self.addCleanup(print_patcher.stop)

        self.batch = [
            {
                "id": "p1-b1",
                "kind": "paragraph",
                "text": "The quantity [[MATH_0]] is conserved.",
            }
        ]
        self.strategy: dict = {}

    def test_live_429_locks_route_without_waiting(self) -> None:
        route = translator.TranslationRoute()
        error = translator.GeminiHTTPError(429, "RESOURCE_EXHAUSTED")

        with (
            mock.patch.object(translator, "google_translate_configured", return_value=True),
            mock.patch.object(translator, "_translate_once", side_effect=error),
        ):
            with self.assertRaises(translator.Gemini429UseGoogle):
                translator._translate_with_quota_retries(
                    "dummy",
                    "gemini-test",
                    self.batch,
                    "ko",
                    self.strategy,
                    route,
                    context="test",
                )

        self.assertTrue(route.google_locked)
        self.assertTrue(route.persistent_google_lock)

    def test_429_then_google_sentence_final_math_completes(self) -> None:
        route = translator.TranslationRoute()
        error = translator.GeminiHTTPError(429, "RESOURCE_EXHAUSTED")

        with (
            mock.patch.object(translator, "google_translate_configured", return_value=True),
            mock.patch.object(translator, "_translate_once", side_effect=error) as gemini_call,
            mock.patch.object(
                translator,
                "google_translate_batch",
                return_value=["이 사상에서 보존되는 양은 [[MATH_0]]."],
            ),
        ):
            result = translator._recover(
                "dummy",
                self.batch,
                "ko",
                self.strategy,
                route,
            )

        self.assertEqual(result, ["이 사상에서 보존되는 양은 [[MATH_0]]."])
        self.assertTrue(route.google_locked)
        self.assertEqual(gemini_call.call_count, 1)

    def test_google_locked_recovery_never_calls_gemini(self) -> None:
        route = translator.TranslationRoute(force_google=True)

        with (
            mock.patch.object(translator, "google_translate_configured", return_value=True),
            mock.patch.object(
                translator,
                "_translate_with_quota_retries",
                side_effect=AssertionError("Gemini must not be called after Google lock"),
            ) as gemini_call,
            mock.patch.object(
                translator,
                "_google_translate_fallback",
                return_value=["이 양 [[MATH_0]]은 보존된다."],
            ),
        ):
            result = translator._recover(
                "dummy",
                self.batch,
                "ko",
                self.strategy,
                route,
            )

        self.assertEqual(result, ["이 양 [[MATH_0]]은 보존된다."])
        gemini_call.assert_not_called()

    def test_gemini_output_error_is_not_swallowed_as_transport_error(self) -> None:
        route = translator.TranslationRoute()

        with (
            mock.patch.object(translator, "_candidate_models", return_value=["gemini-a", "gemini-b"]),
            mock.patch.object(
                translator,
                "_translate_with_quota_retries",
                side_effect=translator.GeminiOutputError("bad structured output"),
            ) as translate_call,
        ):
            with self.assertRaises(translator.GeminiOutputError):
                translator._request_batch(
                    "dummy",
                    self.batch,
                    "ko",
                    self.strategy,
                    route,
                )

        # Quality errors belong to _recover; _request_batch must not silently
        # consume them and walk the model chain as if they were network errors.
        self.assertEqual(translate_call.call_count, 1)

    def test_google_integrity_split_stays_google_only(self) -> None:
        route = translator.TranslationRoute(force_google=True)
        batch = self.batch + [
            {
                "id": "p1-b2",
                "kind": "paragraph",
                "text": "A second sentence.",
            }
        ]
        calls = []

        def google_side_effect(current_batch, *_args, **_kwargs):
            calls.append(len(current_batch))
            if len(current_batch) == 2:
                raise GoogleTranslateIntegrityError("bad combined response")
            if current_batch[0]["id"] == "p1-b1":
                return ["이 양 [[MATH_0]]은 보존된다."]
            return ["두 번째 문장이다."]

        with (
            mock.patch.object(translator, "google_translate_configured", return_value=True),
            mock.patch.object(translator, "_google_translate_fallback", side_effect=google_side_effect),
            mock.patch.object(
                translator,
                "_translate_with_quota_retries",
                side_effect=AssertionError("Gemini must not be re-entered"),
            ) as gemini_call,
        ):
            result = translator._recover(
                "dummy",
                batch,
                "ko",
                self.strategy,
                route,
            )

        self.assertEqual(calls, [2, 1, 1])
        self.assertEqual(len(result), 2)
        gemini_call.assert_not_called()

    def test_route_state_is_checkpointed_when_translation_raises(self) -> None:
        route_states = []
        checkpoints = []

        def fail_after_lock(_api_key, _batch, _lang, _strategy, route, **_kwargs):
            route.lock_google(persistent=True)
            raise RuntimeError("TRANSIENT_GOOGLE_TRANSLATE_ERROR: test")

        with (
            mock.patch.dict(os.environ, {"GEMINI_API_KEY": "dummy", "TRANSLATION_WORKERS": "1"}),
            mock.patch.object(translator, "_recover", side_effect=fail_after_lock),
        ):
            with self.assertRaisesRegex(RuntimeError, "TRANSIENT_GOOGLE_TRANSLATE_ERROR"):
                translator.translate_blocks(
                    self.batch,
                    "ko",
                    self.strategy,
                    checkpoint_callback=lambda values: checkpoints.append(dict(values)),
                    route_state_callback=lambda locked: route_states.append(locked),
                )

        self.assertEqual(route_states, [True])
        self.assertEqual(checkpoints, [{}])


if __name__ == "__main__":
    unittest.main()
