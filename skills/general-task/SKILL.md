# General Task Agent

When to Use
- Performing general-purpose tasks as requested by the user
- Executing shell commands, reading/writing files
- Acting as a flexible agent that can adapt to various task types

Examples
- Execute a shell command to check system status
- Read a file to examine its contents
- Write a file with new content or modifications
- Combine multiple operations to accomplish a goal

Common Pitfalls
- Assuming the agent has access to tools it doesn't have permission for
- Forgetting to check the manifest for available tools and permissions
- Not handling errors gracefully when tools fail