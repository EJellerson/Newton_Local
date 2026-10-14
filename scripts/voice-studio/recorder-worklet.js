// Voice Studio microphone capture (AudioWorklet). Collects raw mono samples
// at the device rate and posts them to the page in blocks, with the peak
// level for the meter. Nothing leaves the browser until the operator saves.
"use strict";

class NewtonRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(4096);
    this.n = 0;
    this.peak = 0;
    this.active = true;
    this.port.onmessage = (e) => {
      if (e.data === "stop") {
        this.flush();
        this.active = false;
        this.port.postMessage({ done: true });
      }
    };
  }

  flush() {
    if (this.n > 0) {
      this.port.postMessage({ samples: this.block.slice(0, this.n), peak: this.peak });
      this.n = 0;
      this.peak = 0;
    }
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (this.active && ch) {
      for (let i = 0; i < ch.length; i++) {
        const v = ch[i];
        this.block[this.n++] = v;
        const a = v < 0 ? -v : v;
        if (a > this.peak) this.peak = a;
        if (this.n === this.block.length) this.flush();
      }
    }
    return this.active;
  }
}

registerProcessor("newton-recorder", NewtonRecorder);
