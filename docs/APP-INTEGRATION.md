# Integracao Dual Agents com o Codex App

O Codex App permanece como interface conversacional, Architect e autoridade de
revisao. O role `executor` usa exclusivamente o Google Antigravity/Gemini pelo
`agy` headless com `stream-json`; o fluxo ativo nao faz fallback para um
Executor Codex. Os backends Codex existentes permanecem somente para
compatibilidade do Architect e de comandos legados.

## Fluxo diario

1. Abra o Codex App.
2. Abra o projeto alvo.
3. Diga: `Use Dual Agents to implement this task.`
4. O App inspeciona o alvo e prepara um pedido JSON preciso.
5. O App chama `scripts/dual-codex.ps1 delegate` e aguarda a linha final.
6. O App le `result.json`, `executor_report_file`, `git_status` e `diff_file`.
7. O App revisa a implementacao real e apresenta o resultado.
8. Para um finding blocking ou important concreto, o App cria um pedido
   `correct` ligado por `parent_request_id`. Nao ha correcao automatica sem
   essa evidencia.

O App deve consultar `status --json` antes de delegar quando precisar confirmar
role, label, repositorio, Git, a versao do Codex e a versao/status do `agy`.
Delegacao e recusada se o executor nao estiver configurado como `antigravity`,
se o `agy` nao passar o probe de versao ou se a arvore canonica
`C:\\CodexGlobal\\AGENTS.md` / `C:\\CodexGlobal\\skills` estiver indisponivel.

## Pedido minimo

```json
{
  "schema_version": 1,
  "request_id": "feature-001",
  "action": "implement",
  "repository": "C:/Projects/Target",
  "task": "# Implementacao\n...",
  "constraints": ["Do not edit unrelated files"],
  "authorization": {"allowed_actions": []},
  "context_files": [],
  "review_findings": [],
  "max_correction_cycles": 0
}
```

Para uma missao explicitamente autorizada, substitua a lista vazia pelas
acoes exatas, por exemplo `local_commit`, `normal_push`, `create_branch`, `draft_pr_create` e
`draft_pr_update`; nao inclua capacidades nao necessarias.

O pedido `correct` reutiliza o texto original da tarefa e inclui, por exemplo:

```json
{
  "schema_version": 1,
  "request_id": "feature-001-correction-1",
  "parent_request_id": "feature-001",
  "action": "correct",
  "repository": "C:/Projects/Target",
  "task": "# Implementacao\n...",
  "review_findings": [
    {"severity": "blocking", "title": "Teste falha", "details": "..."}
  ]
}
```

O App nunca deve chamar novamente seu proprio perfil visivel por `codex exec`,
ler `auth.json`, ou afirmar sucesso sem ler o diff. Publicacao permanece
negada por padrao; quando o pedido inclui autorizacao explicita, somente as
acoes listadas em `authorization.allowed_actions` podem ser solicitadas.
Autorizacao para `normal_push` nao inclui `force_push`, e autorizacao para
Draft PR nao inclui merge, release, tag ou deploy.

No App Server, a politica `windows.sandbox` vem exclusivamente do `config.toml`
do `CODEX_HOME` da conta. O adapter nao injeta `unelevated` nem usa
`danger-full-access` como fallback. Em Windows, ele consulta `config/read` e,
quando a politica efetiva e `elevated`, consulta `windowsSandbox/readiness`; um
estado diferente de `ready` encerra a delegacao com instrucoes para executar,
em terminal administrativo, `codex sandbox setup --elevated --current-user
--codex-home "<CODEX_HOME>"`. Sem configuracao explicita, o valor permanece
`unspecified` e o default do Codex e preservado, sem provisioning automatico.
Mappings de thread carregam a politica efetiva e sao invalidados quando ela
muda.
`create_branch` e uma autorizacao separada: exige que a branch remota esteja
ausente e nunca atualiza uma branch existente.

Operacoes autenticadas de GitHub nao sao executadas dentro do sandbox do
Executor. O control plane pode encaminhar uma solicitacao tipada ao broker
host-side (`dual-codex publish`), que reutiliza essa mesma allow-list,
verifica o repositorio e aplica o CAS de SHA remoto sem transportar tokens,
headers ou credential-store data para o Executor.

## Executor headless e memoria

O `agy` recebe um evento de usuario por stdin e devolve eventos NDJSON
incrementais (`init`, `step_update` e `result`). O resultado inclui o
`conversation_id`, o estado terminal e o relatorio estruturado; timeout,
cancelamento, stderr, JSON invalido e falha de autenticacao sao reportados sem
serem convertidos em sucesso.

O Executor le apenas o contexto de memoria relevante fornecido pelo Architect.
Ele nao recebe um caminho de escrita para a memoria canonica Obsidian/LVault;
`memory_updates` sao candidatos tipados que o Architect deve verificar,
deduplicar e promover (ou rejeitar) silenciosamente.
