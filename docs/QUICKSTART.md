# Quick Start

Use the Anchorage Municipal Code MCP server from Claude in about two minutes.
Nothing needs to be installed or deployed: the server is already hosted.

To run or deploy your own copy instead, see [Getting Started](GETTING_STARTED.md).

## Step 1: Add the connector

The steps are the same on Claude.ai and Claude Desktop:

1. Go to **Settings** → **Connectors** (or **Customize** → **Connectors** on claude.ai)
2. Click **Add custom connector**
3. Enter a name, for example "Anchorage Municipal Code"
4. Enter the URL: `https://anchorage-ecode.codeforanchorage.org/mcp`

No account or API key is needed. The server is public and read-only.

## Step 2: Turn it on in a chat

In a conversation, click "+" → Connectors and toggle the connector on.

## Step 3: Ask a question

```
Who is responsible for clearing snow from the sidewalk in front of a house in Anchorage?
```

```
How does the Anchorage code define "chronic animal noise"?
```

```
What uses are allowed in the R-1 zoning district?
```

Claude searches the code, reads the sections it finds, and answers with the
section number and an `https://ecode360.com/<guid>` link so you can check the
source. The third question is answered from a PDF table (Table 21.05-1), which
Claude reads with the `read_attachment` tool.

The answers are the published code text, not legal advice.

## Troubleshooting

### Claude does not use the connector

1. Check the URL ends in `/mcp`
2. Check the connector is added in Settings → Connectors
3. Check it is enabled for the conversation ("+" → Connectors → toggle on)

### Some tools are missing

The server has eleven tools (see the [README](../README.md#tools)). If the
connector was added before a tool was released, disconnect and reconnect it in
Settings → Connectors to refresh the tool list.

### Claude asks for approval on every call

Every tool is read-only. In the connector's settings you can set the tools to
always allow.

## Next steps

- [The eCode plugin](BUILT_IN_PLUGINS.md): what each tool does
- [Getting Started](GETTING_STARTED.md): run the server locally or deploy a copy
- [FAQ](FAQ.md)
- [GitHub Issues](https://github.com/codeforanchorage/anchorage-ecode-mcp/issues)
