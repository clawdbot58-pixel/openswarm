# Filesystem Skill

File and directory operations for agents.

## Commands

- `read <path>` - Read file contents
- `write <path> <content>` - Write file
- `edit <path> <old> <new>` - Edit file
- `delete <path>` - Delete file/directory
- `list <path>` - List directory
- `find <pattern>` - Find files by pattern
- `glob <pattern>` - Glob pattern matching

## Safety

- Paths are relative to workspace
- Delete requires confirmation
- Backup before destructive operations
