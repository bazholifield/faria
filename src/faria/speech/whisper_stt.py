"""faster-whisper implementation of the Transcriber interface (ADR-010).

Model files download from Hugging Face into data/ on first load (~480 MB for
"small"). The model is multilingual, so ONE loaded model serves every language
— per-language routing in TranscriberRegistry exists for languages where
whisper itself is weak (Basque), not for capacity reasons.

Latency note (measured on this machine, WSL2 CPU int8, 2.7s utterance):
"base" ~1.5s warm; "small" 5-9s — hence "base" as the shipped default, and
greedy decoding below (beam_size=1; on "small", word-timestamps alone added
~3s and beam width didn't reliably help). The accuracy lever is model size
(via CUDA when available), not beam width. The server logs per-stage timings
so this can be re-tuned with real numbers.
"""

from __future__ import annotations

import numpy as np

from faria.config import AudioConfig
from faria.speech.stt import Transcriber, TranscriptionResult, WordSpan


class FasterWhisperTranscriber(Transcriber):
    def __init__(self, config: AudioConfig, download_root: str | None = None) -> None:
        # Import here so the CLI text path never pays the ctranslate2 import.
        from faster_whisper import WhisperModel

        self._config = config
        self._model = WhisperModel(
            config.stt_model,
            device=config.stt_device,
            compute_type=config.stt_compute,
            download_root=download_root,
        )

    def transcribe(
        self, audio_pcm: bytes, sample_rate: int, language: str | None
    ) -> TranscriptionResult:
        if sample_rate != 16000:
            raise ValueError("FasterWhisperTranscriber expects 16 kHz PCM (see AudioConfig)")
        audio = np.frombuffer(audio_pcm, dtype=np.int16).astype(np.float32) / 32768.0

        segments, info = self._model.transcribe(
            audio,
            language=language,
            beam_size=1,  # greedy: benchmarked as the right CPU tradeoff (see module docstring)
            word_timestamps=True,  # per-word probabilities feed pronunciation suspicion
            condition_on_previous_text=False,  # utterances are independent; avoids hallucination loops
            # faster-whisper's built-in Silero VAD strips non-speech BEFORE
            # decoding — the single biggest lever against hallucinate-on-silence
            # (ADR-025). Left on by default; config can disable for debugging.
            vad_filter=self._config.stt_vad_filter,
        )

        words: list[WordSpan] = []
        texts: list[str] = []
        seg_no_speech: list[float] = []
        seg_logprob: list[float] = []
        for segment in segments:  # generator — consuming it runs the actual decode
            texts.append(segment.text)
            seg_no_speech.append(segment.no_speech_prob)
            seg_logprob.append(segment.avg_logprob)
            for w in segment.words or []:
                words.append(
                    WordSpan(
                        word=w.word.strip(),
                        start_s=w.start,
                        end_s=w.end,
                        confidence=w.probability,
                    )
                )

        text = " ".join(t.strip() for t in texts).strip()
        overall = float(np.mean([w.confidence for w in words])) if words else 0.0
        return TranscriptionResult(
            text=text,
            language=info.language,
            confidence=overall,
            words=words,
            # No segments at all (Silero stripped everything) reads as silence.
            no_speech_prob=float(np.mean(seg_no_speech)) if seg_no_speech else 1.0,
            avg_logprob=float(np.mean(seg_logprob)) if seg_logprob else -10.0,
        )
