# Integracao Dual Agents com o Codex App

O Codex App e a porta de entrada conversacional. O control plane resolve
Architect, Executor e Reviewer pelos roles configurados, pela matriz de
capabilities do provider e pela disponibilidade real do runtime. A thread
visivel nao precisa possuir internamente os outros atores. Fallback automatico
e opcional, desativado por padrao, e so considera perfis explicitamente
autorizados para o role.

## Fluxo diario

1. Abra o Codex App no projeto alvo e forneca a missao normalmente, inclusive
   como arquivo Markdown anexado.
2. A integracao global reconhece pedidos Dual Agents/Dual Codex, fases
   Architect/Executor/Reviewer independentes, referencias ao lifecycle, ou
   blockers que dependam da disponibilidade desses roles. Nenhuma frase fixa
   e necessaria.
3. A thread visivel localiza o Git root do projeto alvo e chama o launcher
   global instalado com `run --repository <Git root> <task-file>`.
4. `dual-codex run` resolve cada fase da configuracao e consulta capability e
   runtime. A thread nao deduz disponibilidade olhando suas proprias tools.
5. Leia o relatorio, `provenance.json`, o status Git e o diff do repositorio
   alvo. Falhas reais do control plane permanecem fail-closed; nunca ofereca
   single-agent como substituto.

Um actor já iniciado como Architect, Executor ou Reviewer por `run` está dentro
de uma fase vinculada ao schema do control plane. Essa fase conclui apenas seu
papel e nao chama o entrypoint global de forma recursiva.

## Instalacao global, atualizacao e verificacao

A fonte da skill e do roteamento global fica versionada neste repositorio.
Execute uma vez com o caminho do config Dual Agents que ja possui os roles:

```powershell
.\scripts\install-dual-agents-integration.ps1 -ConfigPath <config>
.\scripts\install-dual-agents-integration.ps1 -Verify -ConfigPath <config>
```

O instalador sincroniza a skill e o launcher para `C:\CodexGlobal`, registra
somente os caminhos do checkout/config (nao credenciais), atualiza a secao
Dual Agents de `AGENTS.md`, e pode ser repetido para atualizar sem duplicar
conteudo. `-Verify` nao escreve arquivos e falha se a instalacao divergir da
fonte versionada.

No bootstrap do role `architect`, o App injeta o `AGENTS.md` canonico e o
baseline obrigatorio de skills. Em uma missao unattended, o Architect pode
primeiro ler o briefing ou artefato fornecido como contexto somente leitura;
depois escolhe e le por completo as skills canonicas adicionais aplicaveis,
sem perguntar ao usuario. So entao inspeciona o repositorio ou planeja. A
execucao para em fail-closed se uma skill exigida nao puder ser carregada, e o
plano declara o baseline e todas as skills adicionais carregadas.

Perfis OpenAI-compatible e perfis Claude Code restritos nao podem atuar como
Architect: o dispatcher os recusa antes de montar ou enviar o bootstrap, pois
nao conseguem abrir as skills canonicas selecionadas a partir do briefing. O
plano declara os nomes das skills carregadas; o control plane exige o baseline
e verifica todos os arquivos canonicos contra hashes registrados antes e
depois do despacho. O provenance separa `AGENTS.md` e skills do baseline
injetados inline, skills adicionais escolhidas depois da leitura do briefing e
o catalogo completo. A entrega e marcada como mista quando uma skill adicional
vier por referencia a fonte. Uma skill que mudar durante a missao causa falha
fechada.

A interface mostra somente os roles suportados pelo backend do perfil e
permite remover atribuicoes antigas que ficaram invalidas. O registro tambem
rejeita atribuicoes primarias, fallbacks ou trocas incompatíveis, inclusive ao
alterar o backend de um perfil.

Quando o fluxo completo `dual-codex run` e usado, cada fase resolve o ator
novamente a partir de `[roles]` no momento da chamada. Architect e Reviewer
seguem os perfis configurados (inclusive quando compartilham o mesmo
perfil); Executor tambem pode ser um perfil Codex App Server. A execucao grava
`provenance.json` e a secao `Configured actor routing` do relatorio com
`actor_id`, provider, backend, transporte, `configured_actor=true`,
`primary_actor`, `actual_actor` e `fallback_used`. Nenhuma fase configurada e
satisfeita por um worker generico ou por uma API de subagente nativo.

Para executar essas fases configuradas, o control plane deve chamar `run` e
passar explicitamente o projeto alvo, pois o config registrado pode ter outro
`orchestrator.repository`:

```powershell
.\scripts\dual-codex.ps1 --config <config> run --repository <repo-alvo> <task-file>
```

Nao use `terminal list` ou `terminal start` para iniciar ou validar atores da
missao. Esses comandos administram somente sessoes Codex nativas do backend
`windows`; eles nao sao um launcher universal de perfis. `run` usa o dispatcher
de ator configurado para selecionar o runtime correto, sem substituicao
silenciosa.

O App pode consultar `status --json` para exibir role, label, repositorio, Git,
versao do Codex e versao/status do `agy`. `run` e recusado se o backend nao
suportar o role, se o runtime nao estiver disponivel quando aplicavel ou se a arvore canonica
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
