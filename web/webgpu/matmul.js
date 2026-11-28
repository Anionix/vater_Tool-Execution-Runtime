// task.optional_webgpu: matrix multiplication on WebGPU, with the checks around it.
//
// 006  validateInput() refuses a size outside [1, 512], a length other than n*n, a value that
//      is not finite, and anything but a Float32Array, before a buffer is created.
// 009  gpuMatmul() times the whole round trip: buffer creation, upload, dispatch, readback.
// 010  computeWithRetry() requests a new device at most MAX_DEVICE_RETRIES times after a loss,
//      then computes on the CPU instead; it never retries without end.
// The GPU is not a security boundary (011): its result goes back to the Python host, which
// checks it against the reference before accepting it.

export const MAX_N = 512;
export const MAX_DEVICE_RETRIES = 1;

export function validateInput(n, a, b) {
  if (!Number.isInteger(n) || n < 1 || n > MAX_N) return "invalid_size";
  for (const matrix of [a, b]) {
    if (!(matrix instanceof Float32Array)) return "not_float32";
    if (matrix.length !== n * n) return "invalid_length";
    for (const value of matrix) if (!Number.isFinite(value)) return "not_finite";
  }
  return null;
}

let source = null;

async function shaderSource() {
  if (source === null) source = await (await fetch("matmul.wgsl")).text();
  return source;
}

export async function requestDevice() {
  if (!navigator.gpu) return null;
  const adapter = await navigator.gpu.requestAdapter();
  return adapter ? adapter.requestDevice() : null;
}

export async function adapterInfo() {
  if (!navigator.gpu) return null;
  const adapter = await navigator.gpu.requestAdapter();
  if (!adapter) return null;
  const info = adapter.info;
  return { vendor: info.vendor, architecture: info.architecture, fallback: info.isFallbackAdapter };
}

export async function createPipeline(device) {
  const module = device.createShaderModule({ code: await shaderSource() });
  const messages = (await module.getCompilationInfo()).messages.filter((message) => message.type === "error");
  if (messages.length) throw new Error(`shader: ${messages[0].message}`);
  return device.createComputePipeline({ layout: "auto", compute: { module, entryPoint: "main" } });
}

export async function gpuMatmul(device, pipeline, n, a, b) {
  const problem = validateInput(n, a, b);
  if (problem) throw new RangeError(problem);
  const started = performance.now();
  const bytes = n * n * 4;
  const usage = GPUBufferUsage;
  const left = device.createBuffer({ size: bytes, usage: usage.STORAGE | usage.COPY_DST });
  const right = device.createBuffer({ size: bytes, usage: usage.STORAGE | usage.COPY_DST });
  const output = device.createBuffer({ size: bytes, usage: usage.STORAGE | usage.COPY_SRC });
  const dims = device.createBuffer({ size: 16, usage: usage.UNIFORM | usage.COPY_DST });
  const readback = device.createBuffer({ size: bytes, usage: usage.MAP_READ | usage.COPY_DST });
  device.queue.writeBuffer(left, 0, a);
  device.queue.writeBuffer(right, 0, b);
  device.queue.writeBuffer(dims, 0, new Uint32Array([n, 0, 0, 0]));
  const group = device.createBindGroup({
    layout: pipeline.getBindGroupLayout(0),
    entries: [left, right, output, dims].map((buffer, binding) => ({ binding, resource: { buffer } })),
  });
  const encoder = device.createCommandEncoder();
  const pass = encoder.beginComputePass();
  pass.setPipeline(pipeline);
  pass.setBindGroup(0, group);
  pass.dispatchWorkgroups(Math.ceil(n / 8), Math.ceil(n / 8));
  pass.end();
  encoder.copyBufferToBuffer(output, 0, readback, 0, bytes);
  device.queue.submit([encoder.finish()]);
  await readback.mapAsync(GPUMapMode.READ); // rejects when the device is lost
  const result = new Float32Array(readback.getMappedRange().slice(0));
  readback.unmap();
  for (const buffer of [left, right, output, dims, readback]) buffer.destroy();
  return { result, milliseconds: performance.now() - started };
}

export function cpuMatmul(n, a, b) {
  const c = new Float32Array(n * n);
  for (let i = 0; i < n; i += 1) {
    for (let j = 0; j < n; j += 1) {
      let sum = 0;
      for (let k = 0; k < n; k += 1) sum = Math.fround(sum + Math.fround(a[i * n + k] * b[k * n + j]));
      c[i * n + j] = sum;
    }
  }
  return c;
}

export async function computeWithRetry(n, a, b, { getDevice = requestDevice, maxRetries = MAX_DEVICE_RETRIES } = {}) {
  const problem = validateInput(n, a, b);
  if (problem) return { status: "refused", reason: problem, attempts: 0, losses: [] };
  const losses = [];
  let attempts = 0;
  while (attempts <= maxRetries) {
    attempts += 1;
    const device = await getDevice();
    if (!device) break;
    let lost = null;
    device.lost.then((info) => { lost = info.reason || "lost"; });
    try {
      const pipeline = await createPipeline(device);
      const { result } = await gpuMatmul(device, pipeline, n, a, b);
      await new Promise((resolve) => setTimeout(resolve, 0)); // let a pending loss be observed
      if (lost !== null) throw new Error(`device lost: ${lost}`);
      device.destroy();
      return { status: "gpu", result: Array.from(result), attempts, losses };
    } catch (error) {
      losses.push(String(error && error.message ? error.message : error));
    }
  }
  return { status: "cpu_fallback", result: Array.from(cpuMatmul(n, a, b)), attempts, losses };
}

export async function measure(n, a, b, repeats) {
  const device = await requestDevice();
  const pipelineStarted = performance.now();
  const pipeline = await createPipeline(device);
  const pipelineMs = performance.now() - pipelineStarted;
  const gpu = [];
  let first = null;
  for (let r = 0; r < repeats; r += 1) {
    const { result, milliseconds } = await gpuMatmul(device, pipeline, n, a, b);
    gpu.push(milliseconds);
    if (first === null) first = Array.from(result);
  }
  const cpu = [];
  for (let r = 0; r < repeats; r += 1) {
    const started = performance.now();
    cpuMatmul(n, a, b);
    cpu.push(performance.now() - started);
  }
  device.destroy();
  return { pipelineMs, gpuMs: gpu, cpuJsMs: cpu, result: first };
}

document.body.dataset.ready = "true";
