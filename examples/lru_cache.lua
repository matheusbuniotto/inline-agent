local LRUCache = {}
LRUCache.__index = LRUCache

local function attach(self, node)
    node.next = self.head.next
    node.prev = self.head
    self.head.next.prev = node
    self.head.next = node
end

local function detach(node)
    node.prev.next = node.next
    node.next.prev = node.prev
    node.prev = nil
    node.next = nil
end

local function move_to_front(self, node)
    detach(node)
    attach(self, node)
end

local function evict(self)
    local lru = self.tail.prev
    if lru == self.head then
        return nil
    end
    detach(lru)
    self.items[lru.key] = nil
    self.count = self.count - 1
    return lru.key
end

--- Retrieves a value by key, marking it as most recently used.
-- @param key any
-- @return any|nil
function LRUCache:get(key)
    local node = self.items[key]
    if not node then
        return nil
    end
    move_to_front(self, node)
    return node.value
end

--- Inserts or updates a key-value pair, evicting the LRU item if exceeding capacity.
-- @param key any
-- @param value any
function LRUCache:set(key, value)
    if self.capacity <= 0 then
        return
    end

    local node = self.items[key]
    if node then
        node.value = value
        move_to_front(self, node)
        return
    end

    if self.count >= self.capacity then
        evict(self)
    end

    local new_node = {
        key = key,
        value = value,
        prev = nil,
        next = nil,
    }
    attach(self, new_node)
    self.items[key] = new_node
    self.count = self.count + 1
end

--- Returns the current number of stored items.
-- @return integer
function LRUCache:size()
    return self.count
end

--- Removes an item with the given key from the cache.
-- @param key any
-- @return boolean true if item was removed, false otherwise
function LRUCache:delete(key)
    local node = self.items[key]
    if not node then
        return false
    end
    detach(node)
    self.items[key] = nil
    self.count = self.count - 1
    return true
end

--- Clears all items from the cache.
function LRUCache:clear()
    self.head.next = self.tail
    self.tail.prev = self.head
    self.items = {}
    self.count = 0
end

local M = {}

--- Creates a new LRU cache instance.
-- @param capacity integer Maximum number of items to retain
-- @return table LRUCache instance
function M.new(capacity)
    assert(type(capacity) == "number" and capacity >= 0, "capacity must be a non-negative number")
    capacity = math.floor(capacity)

    local head = {}
    local tail = {}
    head.next = tail
    tail.prev = head

    local instance = {
        capacity = capacity,
        count = 0,
        items = {},
        head = head,
        tail = tail,
    }
    return setmetatable(instance, LRUCache)
end

return M
