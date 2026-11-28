;; task.optional_webassembly: core.checked_add as an approved guest of vater-wasm-abi/1.
;; Input: left u32, right u32 (8 bytes, little-endian; the host refuses values outside u32).
;; Output at offset 512: status u8 (0 = sum, 1 = 32-bit unsigned overflow), sum u32 (5 bytes).
(module
  (memory (export "memory") 1)
  (global $next (mut i32) (i32.const 1024))
  (func (export "alloc") (param $len i32) (result i32)
    (local $start i32)
    (local.set $start (global.get $next))
    (global.set $next (i32.add (global.get $next) (local.get $len)))
    (local.get $start))
  (func (export "run") (param $ptr i32) (param $len i32) (result i64)
    (local $sum i64)
    (if (i32.ne (local.get $len) (i32.const 8)) (then (unreachable)))
    (local.set $sum (i64.add (i64.load32_u (local.get $ptr))
                             (i64.load32_u offset=4 (local.get $ptr))))
    (if (i64.gt_u (local.get $sum) (i64.const 4294967295))
      (then (i32.store8 (i32.const 512) (i32.const 1))
            (i32.store (i32.const 513) (i32.const 0)))
      (else (i32.store8 (i32.const 512) (i32.const 0))
            (i32.store (i32.const 513) (i32.wrap_i64 (local.get $sum)))))
    (i64.or (i64.shl (i64.const 512) (i64.const 32)) (i64.const 5))))
