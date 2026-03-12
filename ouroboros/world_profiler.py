import os
import platform
import shutil

from ouroboros.compat import get_system_memory, get_cpu_info

def generate_world_profile(output_path: str):
    """Generates a WORLD.md file containing the system profile and hardware details."""
    
    os_name = platform.system()
    os_release = platform.release()
    arch = platform.machine()
    
    mem_total = get_system_memory()
    cpu_info = get_cpu_info()
        
    # User and paths
    user = os.environ.get("USER", "unknown")
    cwd = os.getcwd()
    
    # Check for CLI tools
    tools = []
    for tool in ["git", "python3", "python", "pip", "npm", "node", "claude"]:
        if shutil.which(tool):
            tools.append(tool)
            
    content = f"""# WORLD.md — Environment Profile

This is where I currently exist. It defines my hardware, OS, and local constraints.

## System
- **OS**: {os_name} {os_release} ({arch})
- **CPU**: {cpu_info}
- **RAM**: {mem_total}
- **User**: {user}
- **Current Directory**: {cwd}

## Available Tools
The following binaries are available in my PATH:
`{', '.join(tools)}`

## File System Rules
I live inside `~/Ouroboros/`. 
- `repo/` contains my codebase.
- `data/` contains my memory, state, and logs.
I should generally confine my writes to these directories, though I have read access to the rest of the filesystem if needed for exploration.

*(Generated automatically on first boot)*
"""
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

if __name__ == "__main__":
    generate_world_profile("WORLD.md")
