# Code Style and Conventions

## General Style
- **Formatting**: Standard Python style with clear spacing
- **Naming**: snake_case for functions/variables
- **Type Hints**: Not consistently used (optional)
- **Docstrings**: Present for key functions (triple-quoted strings)

## Specific Patterns
- **Async Functions**: Heavy use of `async/await` for I/O operations
- **Error Handling**: Try-except blocks with detailed logging
- **Logging**: Uses Python's logging module with INFO level
- **Constants**: UPPERCASE for configuration values loaded from env
- **Retry Logic**: Exponential backoff pattern for API calls
- **Message Formatting**: Markdown format with emoji indicators:
  - 🚨 New trades
  - ❌ Closed trades
  - ⚠️ Trade updates
  - 🟢 Long positions
  - 🔴 Short positions

## Common Practices
- Load environment variables at module level
- Use global state for subscribers (in-memory set + JSON persistence)
- Format numbers with thousands separators and decimal precision
- Scale values from raw units (USDC 6 decimals, prices 18 decimals)
- Leverage displayed as decimal (e.g., 25.00x)