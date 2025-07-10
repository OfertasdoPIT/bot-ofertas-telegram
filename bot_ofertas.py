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
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = "@ofertasdopit"
SEU_ID_ASSOCIADO = "ofertasdopit1-20" 

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

# --- FUNÇÕES DE SCRAPING (COMPLETAS E ATUALIZADAS) ---

def extrair_asin(url):
    match = re.search(r'/(dp|gp/product)/(\w{10})', url)
    if match: return match.group(2)
    return None

def formatar_link_associado(asin, id_associado):
    if asin: return f"https://www.amazon.com.br/dp/{asin}/?tag={id_associado}"
    return None

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
    # Headers aprimorados para parecer mais com um navegador real
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'DNT': '1',
        'Upgrade-Insecure-Requests': '1',
    }
    try:
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
    
    # --- NOVA LÓGICA DE BUSCA DE PREÇO (MAIS ROBUSTA) ---
    dados['preco_atual_completo'] = None
    dados['preco_original_completo'] = None
    dados['desconto_percentual'] = None

    # Lista de seletores para o PREÇO ATUAL (do mais específico ao mais genérico)
    seletores_preco_atual = [
        '#corePrice_feature_div .a-offscreen',
        '#snsPrice .a-offscreen',
        '#priceblock_ourprice',
        '#priceblock_dealprice',
        '.priceToPay .a-offscreen',
        '.a-price.a-text-price .a-offscreen'
    ]
    for selector in seletores_preco_atual:
        tag = soup.select_one(selector)
        if tag:
            dados['preco_atual_completo'] = tag.get_text(strip=True)
            logger.info(f"Preço atual encontrado com o seletor: {selector}")
            break # Para o loop assim que encontrar o primeiro preço

    # Lista de seletores para o PREÇO ORIGINAL (riscado)
    seletores_preco_original = [
        'span[data-a-strike="true"] .a-offscreen',
        '.basisPrice .a-offscreen',
        '.a-text-strike'
    ]
    for selector in seletores_preco_original:
        tag = soup.select_one(selector)
        if tag:
            dados['preco_original_completo'] = tag.get_text(strip=True)
            logger.info(f"Preço original encontrado com o seletor: {selector}")
            break

    # Se o desconto não foi pego diretamente, calcula
    if not dados['desconto_percentual'] and dados.get('preco_original_completo') and dados.get('preco_atual_completo'):
        preco_original_num = limpar_preco(dados['preco_original_completo'])
        preco_atual_num = limpar_preco(dados['preco_atual_completo'])
        if preco_original_num and preco_atual_num and preco_original_num > preco_atual_num:
            desconto = ((preco_original_num - preco_atual_num) / preco_original_num) * 100
            dados['desconto_percentual'] = f"{int(round(desconto, 0))}% OFF"

    dados['avaliacao'] = soup.find('span', {'data-hook': 'rating-out-of-text'}).get_text(strip=True) if soup.find('span', {'data-hook': 'rating-out-of-text'}) else 'Sem avaliações'
    dados['num_avaliacoes'] = soup.find('span', {'id': 'acrCustomerReviewText'}).get_text(strip=True) if soup.find('span', {'id': 'acrCustomerReviewText'}) else ''
    dados['asin'] = extrair_asin(url_produto)

    return dados

def gerar_mensagem_divulgacao(dados, id_associado):
    if dados.get('erro'): return dados['erro']
    if not dados.get('preco_atual_completo'): return f"Produto '{dados['titulo']}' parece estar indisponível."
    link_afiliado = formatar_link_associado(dados['asin'], id_associado)
    if not link_afiliado: return "Não foi possível gerar o link de associado."

    mensagem = f"🔥 OFERTA IMPERDÍVEL 🔥\n\n"
    mensagem += f"🏷️ *Produto:* {dados['titulo']}\n\n"
    if dados.get('preco_original_completo'): mensagem += f"❌ De: ~{dados['preco_original_completo']}~\n"
    mensagem += f"✅ *Por: {dados['preco_atual_completo']}*\n"
    if dados.get('desconto_percentual'): mensagem += f"🤑 *{dados['desconto_percentual'].replace('-', '')} de desconto!* 🔥\n"
    mensagem += f"\n⭐ *Avaliação:* {dados['avaliacao']} ({dados['num_avaliacoes']})\n\n"
    mensagem += f"🔗 *Compre aqui com seu desconto:*\n{link_afiliado}\n\n"
    mensagem += f"🛒 Estoque limitado! Preços podem mudar a qualquer momento."
    
    return mensagem.strip()

# --- CÉREBRO DO BOT (sem alterações) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Olá! Sou seu robô de ofertas. Me envie um link da Amazon.')

async def processar_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if 'amazon.com.br' not in url:
        await update.message.reply_text('Por favor, envie um link válido da Amazon Brasil.')
        return
    await update.message.reply_text('Entendido! Processando o link, aguarde um momento...')
    try:
        logger.info(f"Processando URL: {url}")
        dados = buscar_dados_produto(url)
        if dados.get('erro'):
            await update.message.reply_text(f"Ocorreu um erro: {dados['erro']}")
            return
        mensagem = gerar_mensagem_divulgacao(dados, SEU_ID_ASSOCIADO)
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
