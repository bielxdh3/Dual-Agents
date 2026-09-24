# Integracao Dual Agents com o Codex App

O Codex App permanece como interface conversacional, Architect e autoridade de
revisao. O role `executor` resolve o perfil configurado: Antigravity/Gemini
ou Codex App Server. Fallback automatico e opcional, desativado por padrao,
e so considera perfis explicitamente autorizados para o role.

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

No bootstrap do role `architect`, o App injeta o `AGENTS.md` canonico e deixa
a selecao de skills para o proprio Architect. Em uma missao unattended, ele
pode ler o briefing ou artefato fornecido em modo somente leitura, escolhe e
le por completo as skills canonicas aplicaveis sem perguntar ao usuario e so
entao inspeciona o repositorio ou planeja. A execucao para em fail-closed se
uma skill exigida nao puder ser carregada.

Perfis OpenAI-compatible e perfis Claude Code restritos nao podem atuar como
Architect: o dispatcher os recusa antes de montar ou enviar o bootstrap, pois
nao conseguem abrir as skills canonicas selecionadas a partir do briefing. O
plano declara os nomes das skills carregadas; o control plane verifica os
arquivos canonicos contra um snapshot de hashes criado antes do despacho. O
provenance registra separadamente o artefato inline de `AGENTS.md`, as fontes
canonicas selecionadas depois da leitura do briefing e o catalogo completo. A
entrega e marcada como mista quando as skills vierem por referencia a fonte.
Uma skill que mudar durante a missao causa falha fechada.

Quando o fluxo completo `dual-codex run` e usado, cada fase resolve o ator
novamente a partir de `[roles]` no momento da chamada. Architect e Reviewer
seguem os perfis configurados (inclusive quando compartilham o mesmo
perfil); Executor tambem pode ser um perfil Codex App Server. A execucao grava
`provenance.json` e a secao `Configured actor routing` do relatorio com
`actor_id`, provider, backend, transporte, `configured_actor=true`,
`primary_actor`, `actual_actor` e `fallback_used`. Nenhuma fase configurada e
satisfeita por um worker generico ou por uma API de subagente nativo.

Para executar essas fases configuradas, o control plane deve chamar `run`:

```powershell
.\scripts\dual-codex.ps1 --config <config> run <task-file>
```

Nao use `terminal list` ou `terminal start` para iniciar ou validar atores da
missao. Esses comandos administram somente sessoes Codex nativas do backend
`windows`; eles nao sao um launcher universal de perfis. `run` usa o dispatcher
de ator configurado para selecionar o runtime correto, sem substituicao
silenciosa.

O App deve consultar `status --json` antes de delegar quando precisar confirmar
role, label, repositorio, Git, a versao do Codex e a versao/status do `agy`.
Delegacao e recusada se o backend nao suportar o role, se o `agy` nao passar o
probe de versao quando aplicavel ou se a arvore canonica
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
`danger-full-access` como fallback. Em uma delegacao Codex `workspace-write`,
ele consulta `config/read` e `windowsSandbox/readiness`; um estado diferente de
`ready` encerra a delegacao antes do turno, com instrucoes para executar,
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
