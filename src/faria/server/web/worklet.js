// Mic capture worklet: forwards raw Float32 blocks to the main thread.
// All downsampling/encoding happens in the page (main thread) — the worklet
// stays trivial because it runs on the realtime audio thread.
class MicCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) {
      // Copy: the engine reuses the underlying buffer between callbacks.
      this.port.postMessage(new Float32Array(channel));
    }
    return true;
  }
}
registerProcessor("mic-capture", MicCaptureProcessor);
