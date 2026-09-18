-- inline_agent.lua
-- Neovim companion plugin for inline spec-driven AI design
-- Installed in ~/.config/nvim/lua/custom/plugins/inline_agent.lua

local M = {
  backend = vim.g.inline_agent_backend or nil,
  model = vim.g.inline_agent_model or nil,
}

local ns_id = vim.api.nvim_create_namespace("inline_agent_ui")
local spinner_ns = vim.api.nvim_create_namespace("inline_agent_spinner")
local active_spinners = {}

-- Find the best line in the buffer to attach the animated inline spinner
local function find_best_spinner_line(bufnr)
  local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  local cursor_line = vim.api.nvim_win_get_cursor(0)[1] - 1 -- 0-indexed

  -- Look backwards from cursor or buffer end for seal [run]
  for idx = #lines, 1, -1 do
    local l = lines[idx]
    if l:match("%[(run|end)") or l:match("%[run:") or l:match("%[test%]") then
      return idx - 1
    end
  end

  -- Look for [ai] directive
  for idx = #lines, 1, -1 do
    local l = lines[idx]
    if l:match("%[ai") then
      return idx - 1
    end
  end

  return math.max(0, cursor_line)
end

-- Start a smooth Braille inline spinner with elapsed execution time
local function start_inline_spinner(bufnr, target_line, label)
  local uv = vim.uv or vim.loop
  if not uv then return function() end end

  -- Stop any existing spinner for this buffer
  if active_spinners[bufnr] then
    pcall(function()
      active_spinners[bufnr].timer:stop()
      active_spinners[bufnr].timer:close()
    end)
    vim.api.nvim_buf_clear_namespace(bufnr, spinner_ns, 0, -1)
    active_spinners[bufnr] = nil
  end

  local frames = { "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏" }
  local frame_idx = 1
  local start_time = uv.now()
  label = label or "synthesizing proposal with AI"

  local timer = uv.new_timer()
  active_spinners[bufnr] = { timer = timer, line = target_line }

  timer:start(0, 80, vim.schedule_wrap(function()
    if not vim.api.nvim_buf_is_valid(bufnr) then
      if timer and not timer:is_closing() then
        timer:stop()
        timer:close()
      end
      active_spinners[bufnr] = nil
      return
    end

    local elapsed = (uv.now() - start_time) / 1000.0
    local spinner_char = frames[frame_idx]
    frame_idx = (frame_idx % #frames) + 1

    vim.api.nvim_buf_clear_namespace(bufnr, spinner_ns, 0, -1)

    local line_count = vim.api.nvim_buf_line_count(bufnr)
    local actual_line = math.min(target_line, line_count - 1)
    if actual_line < 0 then actual_line = 0 end

    pcall(vim.api.nvim_buf_set_extmark, bufnr, spinner_ns, actual_line, 0, {
      virt_text = {
        { "  " .. spinner_char .. " ", "DiagnosticWarn" },
        { "🤖 [inline-agent: " .. label .. "... " .. string.format("%.1fs", elapsed) .. "]", "Comment" },
      },
      virt_text_pos = "eol",
    })
  end))

  return function(success)
    if timer and not timer:is_closing() then
      timer:stop()
      timer:close()
    end
    active_spinners[bufnr] = nil
    if vim.api.nvim_buf_is_valid(bufnr) then
      vim.api.nvim_buf_clear_namespace(bufnr, spinner_ns, 0, -1)
      if success then
        local line_count = vim.api.nvim_buf_line_count(bufnr)
        local actual_line = math.min(target_line, line_count - 1)
        if actual_line >= 0 then
          pcall(vim.api.nvim_buf_set_extmark, bufnr, spinner_ns, actual_line, 0, {
            virt_text = {
              { "  ✨ ✅ [inline-agent: proposal ready!]", "DiagnosticOk" },
            },
            virt_text_pos = "eol",
          })
          local fade_timer = uv.new_timer()
          fade_timer:start(1200, 0, vim.schedule_wrap(function()
            if vim.api.nvim_buf_is_valid(bufnr) then
              vim.api.nvim_buf_clear_namespace(bufnr, spinner_ns, 0, -1)
            end
            fade_timer:close()
          end))
        end
      end
    end
  end
end

-- Detect if cursor is inside a specific conflict review frame
local function get_conflict_range_at_cursor()
  local bufnr = vim.api.nvim_get_current_buf()
  local cursor_line = vim.api.nvim_win_get_cursor(0)[1] -- 1-indexed
  local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)

  local start_idx = nil
  local mid_idx = nil
  local end_idx = nil

  -- Scan upwards to find start marker
  for idx = cursor_line, 1, -1 do
    if lines[idx]:match("^<<<<<<<") then
      start_idx = idx
      break
    end
    if lines[idx]:match("^>>>>>>>") and idx < cursor_line then
      break
    end
  end

  if start_idx then
    for idx = start_idx, #lines do
      if lines[idx]:match("^=======") then
        mid_idx = idx
      elseif lines[idx]:match("^>>>>>>>") then
        end_idx = idx
        break
      end
    end
  end

  if start_idx and mid_idx and end_idx and cursor_line >= start_idx and cursor_line <= end_idx then
    return {
      start_idx = start_idx,
      mid_idx = mid_idx,
      end_idx = end_idx,
      lines = lines,
      bufnr = bufnr,
    }
  end

  return nil
end

-- Run inline-agent CLI with specified arguments
local function run_daemon_cmd(args, on_success_msg)
  local file = vim.api.nvim_buf_get_name(0)
  if file == "" then
    vim.notify("Inline AI: Buffer has no associated file", vim.log.levels.WARN)
    return
  end

  local bufnr = vim.api.nvim_get_current_buf()
  local is_synthesis = false
  for _, a in ipairs(args) do
    if a == "--once" or a == "--suggest" then
      is_synthesis = true
      break
    end
  end

  local stop_spinner = nil
  if is_synthesis then
    local target_line = find_best_spinner_line(bufnr)
    local has_test = false
    for _, a in ipairs(args) do
      if a == "--test" then has_test = true end
    end
    local label = has_test and "synthesizing code & companion tests" or "synthesizing code"
    stop_spinner = start_inline_spinner(bufnr, target_line, label)
  end

  local cmd = { "inline-agent" }
  local active_backend = M.backend or vim.g.inline_agent_backend
  local active_model = M.model or vim.g.inline_agent_model
  if active_backend then
    table.insert(cmd, "--backend")
    table.insert(cmd, active_backend)
  end
  if active_model then
    table.insert(cmd, "--model")
    table.insert(cmd, active_model)
  end
  for _, arg in ipairs(args) do
    table.insert(cmd, arg)
  end
  table.insert(cmd, file)

  vim.system(cmd, { text = true }, function(obj)
    vim.schedule(function()
      if stop_spinner then
        stop_spinner(obj.code == 0)
      end

      if obj.code == 0 then
        vim.cmd("checktime")
        M.refresh_extmarks()
        if on_success_msg then
          vim.notify("✅ " .. on_success_msg, vim.log.levels.INFO)
        end
      else
        vim.notify("❌ Inline AI error:\n" .. (obj.stderr or obj.stdout or "unknown"), vim.log.levels.ERROR)
      end
    end)
  end)
end

function M.propose()
  run_daemon_cmd({ "--once" }, "Proposal generated! Review in buffer.")
end

function M.propose_with_tests()
  run_daemon_cmd({ "--once", "--test" }, "Proposal + verified companion tests generated! Review in buffer.")
end

-- Propose implementation specifically for visually selected lines
function M.propose_selection()
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<Esc>", true, false, true), "n", false)
  vim.schedule(function()
    local s = vim.fn.line("'<")
    local e = vim.fn.line("'>")
    if s > 0 and e >= s then
      run_daemon_cmd({ "--once", "--range", s .. ":" .. e }, "Proposal generated for selected lines " .. s .. "-" .. e .. "!")
    end
  end)
end

function M.propose_selection_with_tests()
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<Esc>", true, false, true), "n", false)
  vim.schedule(function()
    local s = vim.fn.line("'<")
    local e = vim.fn.line("'>")
    if s > 0 and e >= s then
      run_daemon_cmd({ "--once", "--test", "--range", s .. ":" .. e }, "Proposal + tests generated for selected lines " .. s .. "-" .. e .. "!")
    end
  end)
end

function M.propose_with_context()
  vim.ui.input({ prompt = "Constraint Files (comma-separated, e.g. schema.py, types.go): " }, function(input)
    if input and input ~= "" then
      run_daemon_cmd({ "--once", "--context", input }, "Proposal generated with context constraints!")
    end
  end)
end

-- Accept proposal: if cursor is inside a specific block, accept that block. Otherwise accept all.
function M.accept()
  local range = get_conflict_range_at_cursor()
  if range then
    local proposal_lines = {}
    for i = range.start_idx + 1, range.mid_idx - 1 do
      table.insert(proposal_lines, range.lines[i])
    end
    vim.api.nvim_buf_set_lines(range.bufnr, range.start_idx - 1, range.end_idx, false, proposal_lines)
    vim.cmd("write")
    M.refresh_extmarks()
    vim.notify("✅ Accepted proposal under cursor!", vim.log.levels.INFO)
    return
  end
  run_daemon_cmd({ "--accept" }, "Accepted all proposals!")
end

-- Deny proposal: if cursor is inside a specific block, deny that block. Otherwise deny all.
function M.deny()
  local range = get_conflict_range_at_cursor()
  if range then
    local spec_lines = {}
    for i = range.mid_idx + 1, range.end_idx - 1 do
      table.insert(spec_lines, range.lines[i])
    end
    vim.api.nvim_buf_set_lines(range.bufnr, range.start_idx - 1, range.end_idx, false, spec_lines)
    vim.cmd("write")
    M.refresh_extmarks()
    vim.notify("🛑 Denied proposal under cursor. Restored spec.", vim.log.levels.INFO)
    return
  end
  run_daemon_cmd({ "--deny" }, "Denied proposals. Restored spec.")
end

function M.suggest()
  vim.ui.input({ prompt = "Human Suggestion / Feedback: " }, function(input)
    if input and input ~= "" then
      run_daemon_cmd({ "--suggest", "--note", input }, "Re-synthesizing proposal with feedback...")
    end
  end)
end

-- Jump to next proposal conflict marker in buffer
function M.next_proposal()
  local bufnr = vim.api.nvim_get_current_buf()
  local cursor_line = vim.api.nvim_win_get_cursor(0)[1]
  local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  for idx = cursor_line + 1, #lines do
    if lines[idx]:match("^<<<<<<<") then
      vim.api.nvim_win_set_cursor(0, { idx, 0 })
      return
    end
  end
  vim.notify("No next proposal found", vim.log.levels.INFO)
end

-- Jump to previous proposal conflict marker in buffer
function M.prev_proposal()
  local bufnr = vim.api.nvim_get_current_buf()
  local cursor_line = vim.api.nvim_win_get_cursor(0)[1]
  local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  for idx = cursor_line - 1, 1, -1 do
    if lines[idx]:match("^<<<<<<<") then
      vim.api.nvim_win_set_cursor(0, { idx, 0 })
      return
    end
  end
  vim.notify("No previous proposal found", vim.log.levels.INFO)
end

-- Render interactive virtual text directly above conflict frames in Neovim
function M.refresh_extmarks()
  local bufnr = vim.api.nvim_get_current_buf()
  if not vim.api.nvim_buf_is_valid(bufnr) then
    return
  end
  vim.api.nvim_buf_clear_namespace(bufnr, ns_id, 0, -1)

  local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  for idx, line in ipairs(lines) do
    if line:match("^<<<<<<<") then
      vim.api.nvim_buf_set_extmark(bufnr, ns_id, idx - 1, 0, {
        virt_text = {
          { "  [ ✔ Accept: <leader>aa ] ", "DiagnosticOk" },
          { "[ 🛑 Deny: <leader>ad ] ", "DiagnosticError" },
          { "[ 🔄 Suggest: <leader>as ]", "DiagnosticWarn" },
        },
        virt_text_pos = "eol",
      })
    end
  end
end

function M.set_backend(backend)
  M.backend = backend
  vim.g.inline_agent_backend = backend
  vim.notify("🤖 Inline AI backend set to: " .. backend, vim.log.levels.INFO)
end

function M.set_model(model)
  M.model = model
  vim.g.inline_agent_model = model
  vim.notify("🤖 Inline AI model set to: " .. model, vim.log.levels.INFO)
end

function M.setup(opts)
  opts = opts or {}
  if opts.backend then M.set_backend(opts.backend) end
  if opts.model then M.set_model(opts.model) end

  -- Define user commands
  vim.api.nvim_create_user_command("InlinePropose", M.propose, {})
  vim.api.nvim_create_user_command("InlineProposeWithTests", M.propose_with_tests, {})
  vim.api.nvim_create_user_command("InlineProposeSelection", M.propose_selection, {})
  vim.api.nvim_create_user_command("InlineProposeWithContext", M.propose_with_context, {})
  vim.api.nvim_create_user_command("InlineProposeRange", function(opts)
    run_daemon_cmd({ "--once", "--range", opts.line1 .. ":" .. opts.line2 }, "Proposal generated for lines " .. opts.line1 .. "-" .. opts.line2 .. "!")
  end, { range = true })
  vim.api.nvim_create_user_command("InlineAccept", M.accept, {})
  vim.api.nvim_create_user_command("InlineDeny", M.deny, {})
  vim.api.nvim_create_user_command("InlineSuggest", M.suggest, {})
  vim.api.nvim_create_user_command("InlineNextProposal", M.next_proposal, {})
  vim.api.nvim_create_user_command("InlinePrevProposal", M.prev_proposal, {})

  vim.api.nvim_create_user_command("InlineBackend", function(cmd_opts)
    if cmd_opts.args and #cmd_opts.args > 0 then
      M.set_backend(cmd_opts.args)
    else
      local cur = M.backend or vim.g.inline_agent_backend or "auto (agy)"
      vim.notify("Current Inline AI backend: " .. cur, vim.log.levels.INFO)
    end
  end, {
    nargs = "?",
    complete = function()
      return { "agy", "claude", "openai", "codex", "custom" }
    end,
  })

  vim.api.nvim_create_user_command("InlineModel", function(cmd_opts)
    if cmd_opts.args and #cmd_opts.args > 0 then
      M.set_model(cmd_opts.args)
    else
      local cur = M.model or vim.g.inline_agent_model or "auto"
      vim.notify("Current Inline AI model: " .. cur, vim.log.levels.INFO)
    end
  end, { nargs = "?" })

  -- Normal mode keybindings
  local map = vim.keymap.set
  map("n", "<leader>ap", M.propose, { desc = "Inline AI: Propose" })
  map("n", "<leader>at", M.propose_with_tests, { desc = "Inline AI: Propose with Tests" })
  map("n", "<leader>ac", M.propose_with_context, { desc = "Inline AI: Propose with Context Constraints" })
  map("n", "<leader>aa", M.accept, { desc = "Inline AI: Accept (Cursor or All)" })
  map("n", "<leader>ad", M.deny, { desc = "Inline AI: Deny (Cursor or All)" })
  map("n", "<leader>as", M.suggest, { desc = "Inline AI: Suggest / Refine" })
  map("n", "]a", M.next_proposal, { desc = "Inline AI: Next Proposal" })
  map("n", "[a", M.prev_proposal, { desc = "Inline AI: Previous Proposal" })

  -- Visual mode keybindings for selected range
  map("x", "<leader>ap", M.propose_selection, { desc = "Inline AI: Propose for Selection" })
  map("x", "<leader>at", M.propose_selection_with_tests, { desc = "Inline AI: Propose Selection with Tests" })

  -- Auto-render virtual text on buffer read / save
  local group = vim.api.nvim_create_augroup("InlineAgentGroup", { clear = true })
  vim.api.nvim_create_autocmd({ "BufReadPost", "BufWritePost" }, {
    group = group,
    callback = M.refresh_extmarks,
  })
end

-- Automatically initialize when loaded by Neovim
M.setup()

return M
