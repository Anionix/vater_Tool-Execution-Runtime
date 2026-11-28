// task.optional_webgpu.005: C = A x B for square row-major float32 matrices.
// Inputs:  a, b  read-only storage arrays of n*n f32 values (binding 0 and 1)
//          dims  uniform with n, 1 <= n <= 512 (binding 3; checked on the page and the host)
// Output:  c     storage array of n*n f32 values (binding 2)
// Every invocation outside the matrix returns before touching memory (006).

struct Dims {
  n: u32,
}

@group(0) @binding(0) var<storage, read> a: array<f32>;
@group(0) @binding(1) var<storage, read> b: array<f32>;
@group(0) @binding(2) var<storage, read_write> c: array<f32>;
@group(0) @binding(3) var<uniform> dims: Dims;

@compute @workgroup_size(8, 8)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
  let n = dims.n;
  if (id.x >= n || id.y >= n) {
    return;
  }
  var sum: f32 = 0.0;
  for (var k: u32 = 0u; k < n; k = k + 1u) {
    sum = sum + a[id.y * n + k] * b[k * n + id.x];
  }
  c[id.y * n + id.x] = sum;
}
