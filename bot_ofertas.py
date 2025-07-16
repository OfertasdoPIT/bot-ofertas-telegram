import requests
from bs4 import BeautifulSoup
import re
import json
import logging
import os
from threading import Thread
from flask import Flask
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- CONFIGURAÇÃO SEGURA ---
# O token será lido dos "Secrets" do Replit.
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = "@ofertasdopit"
# A variável SEU_ID_ASSOCIADO não é mais necessária, pois você fornecerá o link final.

# Configuração de logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- PARTE PARA MANTER O ROBÔ ACORDADO (KEEP-ALIVE) ---
app = Flask('')

@app.route('/')
def home():
    return "Estou vivo e trabalhando!"

def run_flask():
  app.run(host='0.0.0.0', port=8080)

def start_keep_alive_thread():
    t = Thread(target=run_flask)
    t.start()

# --- FUNÇÕES DE SCRAPING (ATUALIZADAS) ---

def limpar_preco(texto_preco):
    if not texto_preco: return None
    try:
        preco_limpo = re.sub(r'[^\d,]', '', texto_preco).replace(',', '.')
        return float(preco_limpo)
    except (ValueError, AttributeError):
        return None

def baixar_imagem(url_imagem, nome_arquivo="imagem_produto.jpg"):
    if not url_imagem:
        logger.warning("URL da imagem não encontrada. Pulando o download.")
        return False
    try:
        resposta = requests.get(url_imagem, stream=True, timeout=15)
        resposta.raise_for_status()
        with open(nome_arquivo, 'wb') as f:
            for chunk in resposta.iter_content(1024):
                f.write(chunk)
        logger.info(f"Imagem salva com sucesso como '{nome_arquivo}'!")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Erro ao baixar a imagem: {e}")
        return False

def buscar_dados_produto(url_produto):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    }
    try:
        # O requests segue redirecionamentos de links encurtados por padrão
        resposta = requests.get(url_produto, headers=headers, timeout=15)
        resposta.raise_for_status()
    except requests.exceptions.RequestException as e:
        return {'erro': f"Falha ao acessar a URL. Detalhes: {e}"}

    soup = BeautifulSoup(resposta.content, 'html.parser')
    dados = {}

    dados['titulo'] = soup.find('span', {'id': 'productTitle'}).get_text(strip=True) if soup.find('span', {'id': 'productTitle'}) else 'Título não encontrado'

    dados['url_imagem'] = None
    tag_imagem = soup.find('img', {'id': 'landingImage'})
    if tag_imagem:
        if 'data-a-dynamic-image' in tag_imagem.attrs:
            imagens_json = json.loads(tag_imagem.attrs['data-a-dynamic-image'])
            dados['url_imagem'] = list(imagens_json.keys())[0]
        else:
            dados['url_imagem'] = tag_imagem.get('src')
    
    dados['preco_atual_completo'] = None
    dados['preco_original_completo'] = None
    dados['desconto_percentual'] = None

    seletores_preco_atual = [
        '#corePrice_feature_div .a-offscreen', '#snsPrice .a-offscreen', '#priceblock_ourprice',
        '#priceblock_dealprice', '.priceToPay .a-offscreen', '.a-price.a-text-price .a-offscreen'
    ]
    for selector in seletores_preco_atual:
        tag = soup.select_one(selector)
        if tag:
            dados['preco_atual_completo'] = tag.get_text(strip=True)
            logger.info(f"Preço atual encontrado com o seletor: {selector}")
            break

    seletores_preco_original = ['span[data-a-strike="true"] .a-offscreen', '.basisPrice .a-offscreen', '.a-text-strike']
    for selector in seletores_preco_original:
        tag = soup.select_one(selector)
        if tag:
            dados['preco_original_completo'] = tag.get_text(strip=True)
            logger.info(f"Preço original encontrado com o seletor: {selector}")
            break

    if not dados['desconto_percentual'] and dados.get('preco_original_completo') and dados.get('preco_atual_completo'):
        preco_original_num = limpar_preco(dados['preco_original_completo'])
        preco_atual_num = limpar_preco(dados['preco_atual_completo'])
        if preco_original_num and preco_atual_num and preco_original_num > preco_atual_num:
            desconto = ((preco_original_num - preco_atual_num) / preco_original_num) * 100
            dados['desconto_percentual'] = f"{int(round(desconto, 0))}% OFF"

    dados['avaliacao'] = soup.find('span', {'data-hook': 'rating-out-of-text'}).get_text(strip=True) if soup.find('span', {'data-hook': 'rating-out-of-text'}) else 'Sem avaliações'
    dados['num_avaliacoes'] = soup.find('span', {'id': 'acrCustomerReviewText'}).get_text(strip=True) if soup.find('span', {'id': 'acrCustomerReviewText'}) else ''
    
    return dados

def gerar_mensagem_divulgacao(dados, link_do_usuario):
    """Gera a mensagem final usando o link fornecido pelo usuário."""
    if dados.get('erro'): return dados['erro']
    if not dados.get('preco_atual_completo'): return f"Produto '{dados['titulo']}' parece estar indisponível."

    mensagem = f"🔥 OFERTA IMPERDÍVEL 🔥\n\n"
    mensagem += f"🏷️ *Produto:* {dados['titulo']}\n\n"
    if dados.get('preco_original_completo'): mensagem += f"❌ De: ~{dados['preco_original_completo']}~\n"
    mensagem += f"✅ *Por: {dados['preco_atual_completo']}*\n"
    if dados.get('desconto_percentual'): mensagem += f"🤑 *{dados['desconto_percentual'].replace('-', '')} de desconto!* 🔥\n"
    mensagem += f"\n⭐ *Avaliação:* {dados['avaliacao']} ({dados['num_avaliacoes']})\n\n"
    # AQUI ESTÁ A MUDANÇA: Usamos o link que você enviou
    mensagem += f"🔗 *Compre aqui com seu desconto:*\n{link_do_usuario}\n\n"
    mensagem += f"🛒 Estoque limitado! Preços podem mudar a qualquer momento."
    
    return mensagem.strip()

# --- CÉREBRO DO BOT (ATUALIZADO) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Olá! Sou seu robô de ofertas. Me envie um link encurtado da Amazon (amzn.to/...) e eu preparo o post!')

async def processar_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa o link encurtado da Amazon enviado pelo usuário."""
    link_encurtado = update.message.text
    # Verificação simples para garantir que é um link
    if not link_encurtado.startswith('http'):
        await update.message.reply_text('Isso não parece um link válido. Por favor, envie um link encurtado da Amazon.')
        return

    await update.message.reply_text('Entendido! Processando seu link, aguarde um momento...')
    
    try:
        logger.info(f"Processando URL encurtada: {link_encurtado}")
        dados = buscar_dados_produto(link_encurtado)
        if dados.get('erro'):
            await update.message.reply_text(f"Ocorreu um erro: {dados['erro']}")
            return

        # Passamos o seu link encurtado original para a função que gera a mensagem
        mensagem = gerar_mensagem_divulgacao(dados, link_encurtado)
        
        imagem_path = "imagem_produto.jpg"
        if not baixar_imagem(dados.get('url_imagem'), imagem_path):
            await update.message.reply_text("Não consegui baixar a imagem do produto, postarei apenas o texto.")
            await context.bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=mensagem, parse_mode='Markdown')
            return

        logger.info(f"Enviando post para o canal: {TELEGRAM_CHANNEL_ID}")
        with open(imagem_path, 'rb') as foto:
            await context.bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=InputFile(foto),
                caption=mensagem,
                parse_mode='Markdown'
            )
        
        await update.message.reply_text('✅ Oferta postada com sucesso no seu canal!')

    except Exception as e:
        logger.error(f"Erro inesperado no processamento: {e}")
        await update.message.reply_text(f"Ocorreu um erro geral ao processar o link. Detalhes: {e}")

# --- FUNÇÃO PRINCIPAL (sem alterações) ---
def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("ERRO: O Token do Telegram não foi configurado nos Secrets!")
        return
    start_keep_alive_thread()
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, processar_link))
    application.run_polling()

if __name__ == '__main__':
    main()
