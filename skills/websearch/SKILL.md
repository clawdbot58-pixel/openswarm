# Web Search Skill

Search the web for information using Exa API.

## Commands

- `search <query>` - Search the web
- `find <query>` - Find relevant links

## Usage

```javascript
import { search } from '../../swarm/src/skills/websearch.js';

const results = await search('TypeScript best practices 2024');
console.log(results);
```

## API

Uses Exa Search API: https://exa.ai
