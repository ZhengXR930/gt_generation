// non strict version
var global_v1, global_v2, global_v3, global_v4;
var global_a, global_b, global_c, global_d;
var global_destruct =
    (1, eval)(`(function global_destruct(n)
           {
               var v1, v2, v3, v4;
               var array = [ 1, 2, 3, 4, 5 ];
               var o = { a:1, b:2, c:3, d:4 };
               var a, b, c, d;
               [ global_v1, global_v2,, global_v3, ...global_v4] = array;
               ({ a: global_a, b: global_b, c: global_c, d: global_d } = o);
          })`);

global_destruct(42);
