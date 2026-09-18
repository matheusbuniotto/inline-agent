local M
local ok, mod = pcall(require, "lru_cache")
if ok then
    M = mod
else
    M = dofile("lru_cache.lua")
end

local function test_initialization()
    local cache = M.new(3)
    assert(cache:size() == 0, "Initial size should be 0")
    assert(cache:get("missing") == nil, "Missing key should return nil")
end

local function test_set_and_get()
    local cache = M.new(3)
    cache:set("a", 10)
    assert(cache:size() == 1, "Size should be 1 after inserting 'a'")
    assert(cache:get("a") == 10, "Value for 'a' should be 10")

    cache:set("b", 20)
    cache:set("c", 30)
    assert(cache:size() == 3, "Size should be 3 after inserting 3 keys")
    assert(cache:get("a") == 10)
    assert(cache:get("b") == 20)
    assert(cache:get("c") == 30)
end

local function test_update_existing_key()
    local cache = M.new(2)
    cache:set("x", 1)
    cache:set("y", 2)
    assert(cache:size() == 2)

    cache:set("x", 999)
    assert(cache:size() == 2, "Updating an existing key must not increase size")
    assert(cache:get("x") == 999, "Value should be updated to 999")

    -- Since 'x' was updated, 'y' is the least recently used
    cache:set("z", 3)
    assert(cache:get("y") == nil, "'y' should have been evicted")
    assert(cache:get("x") == 999, "'x' should still be retained")
    assert(cache:get("z") == 3, "'z' should be present")
end

local function test_eviction_order()
    local cache = M.new(3)
    cache:set(1, "one")
    cache:set(2, "two")
    cache:set(3, "three")

    -- Access key 1 to promote it to MRU: recency order becomes [1, 3, 2]
    assert(cache:get(1) == "one")

    -- Insert key 4: least recently used is key 2, so key 2 should be evicted
    cache:set(4, "four")
    assert(cache:size() == 3, "Size should not exceed capacity 3")
    assert(cache:get(2) == nil, "Key 2 should be evicted")
    assert(cache:get(1) == "one", "Key 1 should be retained")
    assert(cache:get(3) == "three", "Key 3 should be retained")
    assert(cache:get(4) == "four", "Key 4 should be retained")
end

local function test_capacity_one()
    local cache = M.new(1)
    cache:set("k1", "v1")
    assert(cache:size() == 1)
    assert(cache:get("k1") == "v1")

    cache:set("k2", "v2")
    assert(cache:size() == 1)
    assert(cache:get("k1") == nil, "k1 should be evicted")
    assert(cache:get("k2") == "v2", "k2 should be present")
end

local function test_capacity_zero()
    local cache = M.new(0)
    assert(cache:size() == 0)
    cache:set("any", 42)
    assert(cache:size() == 0, "Capacity 0 should never store items")
    assert(cache:get("any") == nil)
end

local function test_delete_and_clear()
    local cache = M.new(2)
    cache:set("a", 100)
    cache:set("b", 200)

    assert(cache:delete("a") == true, "delete('a') should return true")
    assert(cache:get("a") == nil, "'a' should be gone")
    assert(cache:size() == 1, "Size should be 1 after deleting 'a'")
    assert(cache:delete("a") == false, "Deleting absent key should return false")

    cache:clear()
    assert(cache:size() == 0, "Size should be 0 after clear")
    assert(cache:get("b") == nil, "'b' should be gone after clear")
end

local function run()
    test_initialization()
    test_set_and_get()
    test_update_existing_key()
    test_eviction_order()
    test_capacity_one()
    test_capacity_zero()
    test_delete_and_clear()
    print("All LRU Cache tests passed successfully.")
end

run()