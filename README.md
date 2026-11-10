# 👑 Leve VIP

Bot de vendas VIP no Telegram com painel web, no estilo ApexVips.
Recebe Pix pelo **SyncPay** (e, opcional, cartão em US$/€ pelo Stripe), libera o acesso ao grupo
na hora e tira quem venceu.

## Instalar (celular Android)

**Antes:** instale o **Termux** pelo [F-Droid](https://f-droid.org/packages/com.termux/)
(a versão da Play Store não funciona).

### 1. Crie o bot
No Telegram, abra o **@BotFather**, mande `/newbot`, escolha nome e usuário e **copie o token**.

### 2. Pegue as chaves do SyncPay
No painel do SyncPay: **Developer → API → gerar chave**, com **"Saque" desmarcado**.
Copie o **ID público** e o **ID privado** (o privado só aparece uma vez).

### 3. Cole no Termux
```
curl -fsSL https://raw.githubusercontent.com/Gabrielbatman266/Leve-bot/main/install.sh | bash
```
Ele instala tudo e pergunta o token, as chaves e a senha do painel. Cada chave é conferida na hora.

### 4. No Telegram
1. Abra o seu bot e mande `/admin SUA_SENHA` (você vira o dono e recebe aviso de cada venda).
2. Adicione o bot como **administrador** do grupo/canal VIP, com **convidar usuários** e **banir usuários**.

Pronto: o bot detecta o grupo, cria o primeiro plano e já abre as vendas.
O painel fica em **http://localhost:8080** no navegador do celular.

### Para não desligar sozinho
- **Xiaomi:** Configurações → Apps → Termux → Economia de bateria → **Sem restrições**, ative a
  **Inicialização automática** e trave o Termux nos apps recentes.
- **Ligar quando o celular reiniciar:** instale o app **Termux:Boot** (F-Droid) e abra uma vez.
- Se o bot cair, ele volta sozinho em 5 segundos. Pix pago enquanto ele estava desligado é liberado quando ele voltar.

## Comandos no Termux

| Comando | O que faz |
|---|---|
| `levebot iniciar` | liga o bot em segundo plano |
| `levebot parar` | desliga |
| `levebot status` | mostra se está rodando |
| `levebot log` | acompanha o log ao vivo |
| `levebot painel` | abre o painel no navegador |
| `levebot configurar` | muda token, chaves ou senha |
| `levebot atualizar` | baixa a versão nova daqui do GitHub |

## O que tem

**No bot:** vários planos (mensal, vitalício…) cada um com seu grupo · Pix com QR Code e copia e cola ·
convite único · **remarketing** de Pix não pago · **downsell** com desconto · **upsell** depois da compra ·
lembrete antes de vencer · remoção automática · `/status` para o cliente.

**No painel:** faturamento (hoje/7/30 dias), saldo SyncPay, conversão · vendas · membros (buscar,
dar dias, remover, liberar manual) · planos · grupos detectados · **links rastreados**
(`t.me/seubot?start=threads_bio`) com vendas por origem · **disparo em massa** · todos os textos e descontos.

## Rodar num servidor (24 h)

O mesmo instalador funciona em Linux (Ubuntu/Debian):
```
sudo apt install -y git python3 python3-venv
curl -fsSL https://raw.githubusercontent.com/Gabrielbatman266/Leve-bot/main/install.sh | bash
```
Para abrir o painel de fora, deixe `PANEL_HOST=127.0.0.1` e use um proxy com HTTPS
(Caddy: `seudominio.com { reverse_proxy localhost:8080 }`). Com `PUBLIC_URL=https://seudominio.com`
no `.env`, o SyncPay avisa o pagamento na hora (webhook). O bot sempre confirma consultando a API,
nunca confia só no aviso.

## Arquivos

| Arquivo | Função |
|---|---|
| `main.py` | liga bot + painel |
| `core.py` | bot: vendas, acesso, remarketing, expiração |
| `panel.py` | painel web |
| `gateways.py` | SyncPay e Stripe |
| `db.py` | banco SQLite e configurações |
| `configurar.py` | assistente de configuração |
| `install.sh`, `levebot`, `rodar.sh` | instalação e controle |
