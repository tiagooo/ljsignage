# Especificação — lj-signage

## 1. Objetivo

Permitir à equipa de marketing, a partir de um painel web central:
- ver que vídeos estão em cada loja e o estado de cada Pi;
- colocar um vídeo novo a passar em todas as lojas, num grupo ou numa loja, com data de início e fim;
- retirar ou reordenar vídeos, sem deslocações e sem mexer nos Pis.

## 2. Situação atual e comportamento do script (`docs/player-script.sh`)

Cada TV tem um Raspberry Pi que corre um script em loop com omxplayer. Consequências para a app:

| Comportamento do script | Consequência para a app |
|---|---|
| `$VIDEOPATH/*` é avaliado no início de cada volta do carrossel | Um vídeo novo entra na volta seguinte, sem reiniciar. Atraso máximo ≈ duração de uma volta. |
| `*` não apanha ficheiros nem pastas começados por `.` | Preparação e uploads em curso ficam em nomes ocultos. |
| `*` apanha tudo o que é visível, incluindo subpastas | A pasta só pode conter vídeos finais. |
| `$entry` sem aspas | Nomes sem espaços nem caracteres especiais. |
| Pasta vazia → `*` literal → omxplayer falha em ciclo | Nunca deixar a pasta vazia (vídeo de reserva obrigatório). |
| Ordem alfabética | Ordem controlada por prefixo numérico `NNN_`. |
| Apagar um vídeo em reprodução não o interrompe (inode aberto) | Remoção segura a qualquer momento. |
| Ficheiro renomeado/apagado depois da leitura da lista é saltado nessa volta | Reordenar faz saltar esse vídeo uma vez. Aceitável; minimizar renomeações. |
| Um ficheiro corrompido pode bloquear o omxplayer | Transcodificar e validar sempre antes de enviar. |

## 3. Âmbito

**MVP:** inventário e estado dos 12 Pis; descoberta da pasta de vídeos; biblioteca com transcodificação; programação por destino e datas; reconciliação por FTP com dry-run; registo de atividade; autenticação.

**Fora de âmbito:** qualquer alteração nos Pis; saber que vídeo está a passar; reiniciar Pis; layouts multi-zona, imagens estáticas ou conteúdos HTML.

## 4. Modelo de dados

- **Device** — `code` (sigla), `store_number`, `name`, `hostname`, `ftp_port`, `video_path` (nulo até ser descoberto e confirmado), `enabled`, `groups`, `max_bytes` (opcional). Estado: `status` (online/offline/erro), `last_seen_at`, `last_reconcile_at`, `last_error`.
- **Group** — `name` (ex.: `todas`, `norte`, `sul`); relação N:N com Device. `todas` é implícito.
- **Video** — `title`, `slug`, `original_filename`, `sha256` (do ficheiro transcodificado), `size_bytes`, `duration_s`, `width`, `height`, `status` (a processar / pronto / falhou), `thumbnail`, `created_by`, `created_at`.
- **Assignment** (programação) — `video_id`, `target_type` (all / group / device), `target_id`, `start_at`, `end_at` (nulo = sem fim), `position` (ordem), `is_fallback`, `created_by`.
- **DeviceFile** (estado conhecido no Pi) — `device_id`, `remote_name`, `video_id` (nulo se legado), `state` (staged / active / legacy), `size_bytes`, `uploaded_at`, `verified_at`.
- **AuditLog** — data, utilizador (ou `system`), dispositivo, ação, detalhe, resultado. Guarda apenas nome e email do utilizador (RGPD).
- **Job** — fila de tarefas do worker (ex.: conversão de vídeo): `type`, `payload`, `status`, `attempts`, `error`, `created_at`, `started_at`, `finished_at`.

## 5. Convenções de ficheiros no Pi

- Pasta de vídeos: `<video_path>/` (por dispositivo).
- Preparação: `<video_path>/.lj-staging/<sha256[:12]>.mp4` (oculta, mesmo sistema de ficheiros → renomear é atómico).
- Upload em curso: `<video_path>/.lj-staging/<sha256[:12]>.mp4.part`.
- Ativo: `<video_path>/<NNN>_<slug>-<sha256[:6]>.mp4`, com `NNN` = posição (passos de 10: 010, 020…).
- `slug`: minúsculas, `[a-z0-9-]`, sem acentos, máximo 40 caracteres.
- Vídeo de reserva (fallback): pelo menos um por dispositivo, sempre presente quando não há mais nada ativo.

## 6. Reconciliação

Corre **apenas no serviço `worker`**, a cada `RECONCILE_INTERVAL_MIN` (omissão: 5) e a pedido ("Aplicar agora" cria uma tarefa na fila). Por dispositivo, com lock (um ciclo de cada vez por Pi) e no máximo `MAX_PARALLEL` Pis em simultâneo (omissão: 3).

1. **Ligar** (timeout 10 s, modo passivo). Falha → `offline`, tentar no ciclo seguinte.
2. **Ler estado atual:** ficheiros visíveis em `video_path` e ficheiros em `.lj-staging`. Se a listagem não mostrar ocultos, usar `LIST -a` / `MLSD` e, em último caso, `SIZE <nome>` a partir do estado em `DeviceFile`.
3. **Calcular o pretendido agora:** atribuições ativas aplicáveis (todas + grupos + dispositivo), ordenadas por `position` e depois `start_at`. Se vazio → fallback.
4. **Calcular a preparação:** atribuições que começam nas próximas `STAGING_LOOKAHEAD_H` horas (omissão: 48) e ainda não estão no Pi.
5. **Planear ações** (planner puro), por esta ordem, para preservar as invariantes:
   1. Upload para preparação: `STOR` `.part` → verificar `SIZE` = tamanho local → renomear para o nome de preparação.
   2. Ativar: renomear de `.lj-staging/` para o nome ativo.
   3. Reordenar: renomear apenas os ficheiros cuja posição mudou.
   4. Remover ativos que deixaram de estar programados (apenas ficheiros geridos pela app), **depois** das ativações, garantindo ≥ 1 ativo.
   5. Limpar preparação: ficheiros já não necessários e `.part` com mais de 24 h.
6. **Executar** (se `DRY_RUN=false` e `enabled=true`), registando cada ação. Parar no primeiro erro desse dispositivo; o ciclo seguinte retoma (idempotência).
7. **Atualizar estado** do dispositivo e de `DeviceFile`.

**Primeira sincronização de um dispositivo:** os ficheiros existentes são registados como `legacy` e **não são apagados**. No painel, o utilizador escolhe entre adotar para a biblioteca (descarregar e reprocessar) ou remover. Ficheiros legados com nomes inválidos são assinalados.

**Previsão de entrada no ar:** mostrar "entra no ar até HH:MM", calculado como próximo ciclo de reconciliação + duração da volta atual (soma das durações dos ativos).

## 7. Descoberta da pasta de vídeos (só leitura)

1. Se `video_path` estiver definido em `config/devices.yaml`, validar que existe e usar.
2. Caso contrário, procurar scripts `*.sh` na pasta de login e um nível abaixo (máx. 64 KB cada), descarregar e extrair `VIDEOPATH="…"`.
3. Caso contrário, listar pastas até 2 níveis com ficheiros de vídeo (`.mp4 .mov .m4v .h264 .mkv .avi`).
4. Mapear o caminho absoluto do script para o caminho FTP: tentar o absoluto; se falhar (FTP em chroot), tentar relativo à pasta de login (`/home/tmagalhaes/Videos` → `Videos`).
5. Mostrar o resultado no painel para confirmação de um administrador. Nunca gravar sem confirmação. Nunca escrever no Pi durante a descoberta.

## 8. Perfil de transcodificação (Pi 1 + omxplayer, TV 1080p)

- MP4, `-movflags +faststart`; H.264 High, nível 4.1, `yuv420p`.
- 1920×1080 (ajustar com barras pretas, sem distorcer); fps de origem até 30, caso contrário 25.
- Bitrate alvo 8 Mb/s, máximo 10 Mb/s.
- Áudio removido por omissão (`AUDIO_ENABLED=false`). Se ativo: AAC-LC 128 kb/s, 48 kHz, estéreo.
- Validar com `ffprobe` (codec, dimensões, duração > 0) antes de marcar como pronto. Gerar miniatura.
- Guardar original e transcodificado em `/data/media`.
- A conversão corre no `worker` (uma de cada vez, para não sobrecarregar o `docker-server`); o upload no painel só cria a tarefa e mostra o progresso.

## 9. Interface (português europeu)

1. **Painel** — grelha de lojas: estado, nº de vídeos ativos, duração da volta, última sincronização, alertas.
2. **Loja** — ativos (por ordem), em preparação, legados, histórico, "Aplicar agora", descoberta/confirmação da pasta.
3. **Biblioteca** — upload por arrastar e largar, estado de processamento, miniatura, duração, onde está programado.
4. **Programação** — criar e editar atribuições (vídeo, destino, início/fim, posição); lista e vista de calendário.
5. **Plano** — o que a próxima reconciliação vai fazer em cada loja (essencial enquanto `DRY_RUN=true`).
6. **Atividade** — registo de auditoria filtrável.

Papéis: `admin` (dispositivos, descoberta, dry-run, piloto) e `editor` (biblioteca e programação).

## 10. Configuração

`.env` (ver `.env.example`): `FTP_USER`, `FTP_PASSWORD`, sobreposição por dispositivo `FTP_PASSWORD__<CODE>`, `DRY_RUN`, `RECONCILE_INTERVAL_MIN`, `STAGING_LOOKAHEAD_H`, `MAX_PARALLEL`, `AUDIO_ENABLED`, `TZ`, `DATA_DIR`, `SECRET_KEY`, variáveis de autenticação.

`config/devices.yaml`: lista de dispositivos e grupos (ver `config/devices.example.yaml`). Carregado no arranque e sincronizado para a base de dados.

## 11. Deploy

- `docker compose` no host `docker-server`; uma imagem (Python 3.12 + ffmpeg) e dois serviços:
  - `web`: gunicorn (`lj_signage:create_app()`), 2 workers, porta interna 8080;
  - `worker`: `python -m lj_signage.worker` (agendador, reconciliação, conversões, backups).
- Volume partilhado `/data` (SQLite, media, miniaturas, backups).
- O contentor tem de resolver `*.lugardajoia.internal`: definir `dns` (DNS internos da LJ, ver §14) e `dns_search: [lugardajoia.internal]`.
- FTP em modo passivo (ligações de saída do contentor para os Pis). Não usar modo ativo.
- Exposição fora da rede interna só através de proxy com autenticação.
- Backup diário da base de dados (API de backup do SQLite) para `/data/backups`, com retenção de 30 dias. Procedimento de reposição documentado no README e testado uma vez.
- Healthcheck HTTP (`/health`) no `web`; heartbeat do `worker` na base de dados, visível no painel. Reinício automático (`restart: unless-stopped`) em ambos.

## 12. Fases e critérios de aceitação

- **F0 — Base e inventário (só leitura).** App Flask em Docker (`web` + `worker`), comandos `flask devices sync` e `flask discover`, `devices.yaml` carregado, estado online/offline de todos os Pis, descoberta da pasta, listagem do conteúdo atual. *Aceite quando:* as lojas aparecem no painel com a pasta identificada e confirmada, sem qualquer escrita nos Pis.
- **F1 — Biblioteca.** Upload, transcodificação, validação, miniatura. *Aceite quando:* um MOV/ProRes sai em MP4 conforme §8 e passa no `ffprobe`.
- **F2 — Programação, planner e dry-run.** Atribuições, importação de legados, página "Plano". *Aceite quando:* os testes do planner (incluindo o simulador `tests/player_sim.py` e a mudança de hora de 25/10/2026) cobrem todas as invariantes do CLAUDE.md e o plano é coerente para todas as lojas.
- **F3 — Piloto.** `DRY_RUN=false` apenas no Pi piloto. Validar na TV: entrada de vídeo novo, ativação agendada, fim de campanha, reordenação e fallback. Confirmar listagem de ocultos e renomeação entre pastas nesse servidor FTP.
- **F4 — Rollout e alertas.** Todas as lojas ativas; notificação (Slack) quando um Pi está offline há mais de 30 min ou falha 3 ciclos seguidos.

## 13. Limitações conhecidas

- Não é possível saber que vídeo está a passar nem se o omxplayer está a funcionar; "online" significa apenas que o FTP responde.
- O FTP não indica espaço livre de forma fiável: usar `max_bytes` por dispositivo e o total enviado pela app.
- O FTP transmite credenciais sem encriptação: uso restrito à rede interna.
- Precisão de ativação limitada ao intervalo de reconciliação + duração de uma volta.

## 14. Pontos em aberto

1. As credenciais FTP são iguais em todos os Pis?
2. Software FTP dos Pis (vsftpd, proftpd…): listagem de ocultos e `RNFR/RNTO` entre pastas — validar na F0/F3.
3. Capacidade dos cartões SD por Pi (para `max_bytes`).
4. As TVs passam som?
5. Resolução das TVs (assumido 1080p).
6. Autenticação: Google OAuth restrito a `@lugardajoia.com` (proposta) ou outra.
7. Grupos de lojas pretendidos (os de `devices.example.yaml` são exemplo).
8. Pi piloto para a F3.
9. O volume `/data` está incluído nos backups do `docker-server`?
10. DNS internos para o contentor (assumido 10.0.2.2 e 10.0.2.20).
