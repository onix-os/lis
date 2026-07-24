const std = @import("std");
const lis = @import("document.zig");

test "parse LIS version" {
    const json_text =
        \\{
        \\  "lis": "0.1.0",
        \\  "system": { "hostname": "tron" }
        \\}
    ;

    var gpa = std.heap.GeneralPurposeAllocator(.{}){};
    defer _ = gpa.deinit();
    const allocator = gpa.allocator();

    const parsed = try lis.parseJson(allocator, json_text);
    defer parsed.deinit();

    try std.testing.expectEqualStrings("0.1.0", parsed.value.lis);
}
