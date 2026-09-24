# Referencia CLI — Dual Agents

## Missao com roles configurados

Use o comando `run` para iniciar uma missao completa. Cada fase (Architect,
Executor e Reviewer) e resolvida pelo role configurado e encaminhada ao
runtime do backend correspondente:

```powershell
dual-codex --config <config> run <task.md>
```

`terminal start` e `terminal list` administram sessoes Codex nativas do backend
`windows`; nao use esses comandos como launcher ou verificador universal de
atores configurados. Confira `provenance.json` no diretorio da execucao para
ver o ator e backend selecionados em cada fase.

## Delegacao

```text
python -m dual_codex.cli --config <config> delegate \
  --request-file <request.json> --result-file <result.json>
python -m dual_codex.cli --config <config> delegate \
  --stdin --result-file <result.json>
```

O launcher equivalente no Windows e `scripts/dual-codex.ps1`. Ele usa o Python
do ambiente virtual ativo, depois `.venv`, depois o Python disponivel, injeta o
`src` local em `PYTHONPATH` e funciona em caminhos com espacos.

Selecao do repositorio, em ordem deterministica:

1. `--repository`, quando informado;
2. `repository` no pedido JSON;
3. nenhum alvo: a delegacao e recusada. O `repository` da configuracao serve
   para `status`, `doctor` e o fluxo completo `run`, mas nao e um fallback
   silencioso para `delegate`.

Opcoes adicionais:

- `--allow-dirty`: permite explicitamente um repositorio sujo;
- `--result-file`: recebe uma escrita atomica do resultado;
- `--config`: seleciona a configuracao e os roles.

O comando imprime transicoes `[1/5]` a `[5/5]`, duracao, repositorio resolvido,
diretorio da execucao e uma linha final `DUAL_CODEX_RESULT` com JSON compacto.
O resultado persistido tambem registra o campo `repository` com o mesmo caminho
canonico usado para o Git e para o processo Executor.
O `delegate` aceita o backend configurado para o role `executor`, incluindo
Codex App Server e Antigravity/Gemini. Fallback automatico permanece desligado
por padrao; quando habilitado, somente perfis com `fallback_roles = ["executor"]`
podem ser considerados e no maximo um candidato e tentado. O stdout/stderr do
Executor nao e repassado ao usuario; logs de diagnostico sao sanitizados no
diretorio da execucao.

## Schemas

- [delegation-request.schema.json](../schemas/delegation-request.schema.json)
- [delegation-result.schema.json](../schemas/delegation-result.schema.json)
- [delegation-report.schema.json](../schemas/delegation-report.schema.json)
- [publication-request.schema.json](../schemas/publication-request.schema.json)
- [publication-result.schema.json](../schemas/publication-result.schema.json)

Um pedido `implement` contem `schema_version: 1`, `request_id`, `action`,
`repository` e `task`. `authorization`, `constraints`, `context_files` e
`max_correction_cycles` sao opcionais. `authorization.allowed_actions` e uma
allow-list explicita e fechada, com negacao por padrao; cada acao deve ser
autorizada separadamente. Um pedido `correct` tambem exige
`parent_request_id` e uma lista de findings com `title` e `details`.

O resultado pode ter `completed`, `failed`, `invalid_request`,
`executor_unavailable` ou `cancelled`. Ele aponta para o report do executor, o
stderr sanitizado, o estado do Git e o diff preservado.

`dual-codex run` resolve Architect, Executor e Reviewer a partir de `[roles]`
em cada fase. A execucao registra `provenance.json`; a identidade do perfil e
o backend configurados sao vinculados ao transporte antes da chamada e nao
podem ser substituidos pelo texto da tarefa.

## Operacoes existentes

```text
dual-codex status [--json]
dual-codex dashboard [--port PORT] [--no-open]
dual-codex doctor
dual-codex run task.md
dual-codex publish --request-file publication.json --result-file publication-result.json
dual-codex account ...
dual-codex role ...
```

`publish` e o broker local de host para operacoes tipadas de publicacao. Ele
aceita somente `normal_push`, `create_branch`, `draft_pr_create` ou `draft_pr_update`, valida a
allow-list de autorizacao existente, a identidade do repositorio e a
autenticacao GitHub do host, e nunca recebe tokens. O push usa Git OpenSSL com
verificacao de certificados, exige SHA remoto esperado, descendencia
fast-forward e revalida o SHA depois da operacao. `create_branch` exige estado
remoto ausente, valida a branch e publica somente o SHA exato em
`refs/heads/...`; uma branch existente nunca e atualizada. O comando nao aceita shell,
force-push, merge, release, tag, deploy ou mutacao destrutiva. O App Server nao
recebe acesso ao credential store; o broker deve ser chamado pelo control
plane confiavel no contexto normal do host.

`dual-codex dashboard` serve uma interface Dual Agents local em `127.0.0.1` (porta livre
por padrão) e abre o navegador, salvo com `--no-open`. O painel usa capacidades
do provider selecionado: `model/list` para Codex App Server, o catálogo local
`agy models` para Antigravity/Gemini e capacidades declaradas para API
OpenAI-compatible. O catálogo Antigravity agrupa variantes de esforço em um
modelo lógico e preserva o slug exato para a invocação; modos únicos como
`Thinking` ficam fixos, sem alternativas inventadas. `model = ""` significa
default do provider (modelo e esforço herdados), e as alterações
de model/reasoning/service tier são persistidas atomicamente para turnos futuros
em `config.toml`. A thread atual nunca é alterada silenciosamente.

A seção **Profiles / Accounts** gerencia metadados de contas diretamente no
dashboard: criar, renomear, habilitar/desabilitar e remover o registro sem apagar
o estado do provider. Perfis Codex usam `CODEX_HOME` isolado e expõem ações
provider-native de status, autenticação, reautenticação e logout; o usuário
conclui manualmente qualquer navegador ou MFA iniciado pelo CLI.

Perfis API usam `auth_reference = "env:VARIAVEL"` e nunca armazenam a chave no
TOML ou no dashboard. HTTPS é obrigatório para endpoints remotos; HTTP é aceito
somente em loopback para testes. O adapter envia o modelo/esforço selecionados
ao endpoint `/chat/completions`. O role `executor` continua exigindo
Antigravity/Gemini por política de segurança do produto.

A remoção de uma conta altera apenas o registro por padrão. `--delete-profile`
é uma ação explícita sobre o diretório local controlado pelo perfil e pode
remover o `auth.json` mantido naquele `CODEX_HOME`; keyrings e credenciais
externas não são tocados.

### Live Executor

A aba `EXECUTOR LIVE` observa o Executor real por meio do journal JSONL escrito
pela delegacao/Antigravity. O dashboard nao inicia um processo `agy` proprio para
essa tela. O journal e escolhido no servidor a partir de `runs_dir`, da conta e
role `executor` e da identidade do repositorio; nenhum caminho de arquivo vindo
do navegador e aceito.

O snapshot e o SSE sao limitados por `live_event_journal_max_records`,
`live_event_journal_max_record_bytes` e `live_event_journal_max_detail_bytes`.
O stream suporta replay inicial, cursor ou `Last-Event-ID`, IDs monotonicamente
crescentes, heartbeats e reconexao com backoff. A UI tambem limita as linhas
mantidas no navegador; `Clear View` nao remove o journal nem altera o servidor.

Eventos passam por sanitizacao antes da persistencia: segredos, caminhos
sensiveis, arquivos de autenticacao e reasoning interno sao removidos ou
redigidos. A UI usa insercao textual, nao HTML executavel, e nao exibe
chain-of-thought. Commands, output, diffs, files e mensagens so aparecem com
evidencia protocolar; nao ha inferencia de leituras de arquivo.

O dashboard continua preso a `127.0.0.1` e valida Host/Origin. GET e SSE sao
somente leitura; nao existe shell interativo, browsing arbitrario de arquivos,
CORS ou listener remoto nessa funcionalidade.

## Terminais nativos persistentes

O backend Windows usa um pequeno host Node com `node-pty` sobre ConPTY. Cada
sessao recebe um `CODEX_HOME`, repositorio, PID e log separados; a comunicacao
de controle usa uma named pipe local e nao abre porta de rede.
O host desativa a integracao opcional `apps` do CLI para que o TUI nao dependa
do MCP `codex_apps` do Desktop.

```text
dual-codex terminal start architect-account --role architect --attach
dual-codex terminal start executor-account --role executor --attach
dual-codex terminal start executor-account --role executor --headless
dual-codex terminal list --json
dual-codex terminal send <session-id> "mensagem de follow-up"
dual-codex terminal attach <session-id>
dual-codex terminal terminate <session-id>
```

O modo `dual-codex terminal attach <session-id> --interactive` usa o ConPTY
registrado, saida live por cursor e encaminhamento raw de teclas. `Ctrl-]`
desanexa sem terminar a sessao; quando a automacao possui o lease de entrada,
o humano fica viewer-only. Composicao humana permanece protegida; depois de
inatividade o lease pode expirar e ser readquirido atomicamente na proxima
tecla. Comandos `/model` e `/reasoning` liberam o lease quando o prompt ocioso
volta. O attach padrao permanece o snapshot legado.

Ao iniciar um `executor`, Dual Codex abre automaticamente uma janela visivel
com `terminal attach --interactive` ligada ao mesmo host ConPTY. Esse cliente
nao cria outro processo Codex; `terminal list --json` exibe o host PID, processo
Executor, viewer PID, epoch e named pipe. `--headless` desativa a janela para
fluxos que nao exigem interacao humana. `--reuse-existing` exige esse viewer e
as identidades correspondentes de sessao, conta, `CODEX_HOME` e repositorio.

Para exigir a reutilizacao exata, sem start ou fallback silencioso:

```powershell
dual-codex delegate --request-file request.json --result-file result.json --reuse-existing
```

Essa opcao exige o `executor` nativo Windows registrado e pronto, com a mesma
conta, `CODEX_HOME`, repositorio, role, viewer e identidade host/PID/epoch. Ausencia,
stale, busy ou pipe inalcançavel falham fechado. Uma TUI aberta externamente
nao pode ser adotada.

No resultado, `commands_run: []` significa apenas que o executor nao forneceu
telemetria de comandos; nao e uma falha. Os campos semanticos continuam
obrigatorios e valores presentes com tipo invalido falham na validacao. Em
`reuse_provenance`, conta, role, repositorio, `CODEX_HOME`, sessao, pipe, viewer
e os campos `target_model`/`target_reasoning` sao evidencias do wrapper/TUI;
texto produzido pelo modelo nao pode substitui-los. Modelo ou raciocinio que
nao possam ser observados com seguranca aparecem como `unknown`, com
proveniencia `unavailable`.

`--attach` mantém a saída do TUI visível no terminal atual. O comando
`delegate` usa a sessão persistente do executor por conta e repositório,
mantendo o contexto para correções subsequentes. A dependência Node é
instalada com `npm install`; os testes Python não exigem uma conta Codex real.
