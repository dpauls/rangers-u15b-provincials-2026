#!/usr/bin/env python3
"""
Replay an LLM prompt from a saved file against the Claude API.

Usage:
  # Extract a prompt from the log (copy the PROMPT section to a file):
  python3 scripts/replay_prompt.py prompt_file.txt

  # Or pipe it:
  cat prompt_file.txt | python3 scripts/replay_prompt.py -

The file should contain just the prompt text (no headers/separators).
"""
import sys
import os

def main():
    if len(sys.argv) < 2:
        print('Usage: replay_prompt.py <prompt_file.txt>')
        print('  Extract a PROMPT section from logs/llm_interactions.log,')
        print('  save to a text file, edit as desired, then replay.')
        sys.exit(1)

    path = sys.argv[1]
    if path == '-':
        prompt = sys.stdin.read()
    else:
        with open(path) as f:
            prompt = f.read()

    print(f'Prompt length: {len(prompt)} chars')
    print(f'First 200 chars:\n{prompt[:200]}...\n')

    key = os.environ.get('ANTHROPIC_API_KEY')
    if not key:
        print('ERROR: ANTHROPIC_API_KEY not set')
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic(api_key=key)

    print('Calling Claude...\n')
    resp = client.messages.create(
        model='claude-sonnet-4-20250514',
        max_tokens=600,
        messages=[{'role': 'user', 'content': prompt}],
    )

    result = resp.content[0].text
    print('=' * 60)
    print('RESPONSE:')
    print('=' * 60)
    print(result)
    print('=' * 60)
    print(f'\nTokens: input={resp.usage.input_tokens}, output={resp.usage.output_tokens}')


if __name__ == '__main__':
    main()
