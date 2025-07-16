import requests
from bs4 import BeautifulSoup
import re
import json
import logging
import os
import time
from threading import Thread
from flask import Flask
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# --- CONFIGURAÇÃO SEGURA ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = "@ofertasdopit"

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
    return "Estou vivo e trabalhando com Selenium!"
def run_flask():
  app.run(host='0.0.0.0', port=8080)
def start_keep_alive_thread():
    t = Thread(target=run_flask)
    t.start()

# --- FUNÇÕES DE SCRAPING (COMPLETAS) ---

def baixar_imagem(url_imagem, nome_arquivo="imagem_produto.jpg"):
    if not url_imagem:
        logger.warning("URL da imagem não encontrada.")
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
    logger.info("Iniciando busca de dados com Selenium...")
    chrome_options = Options()
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    
    service = Service()
    driver = webdriver.Chrome(service=service, options=chrome_options)
    
    dados = {}
    try:
        driver.get(url_produto)
        logger.info("Aguardando página carregar...")
        time.sleep(5)
        
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        
        dados['titulo'] = soup.find('span', {'id': 'productTitle'}).get_text(strip=True) if soup.find('span', {'id': 'productTitle'}) else 'Título não encontrado'

        dados['url_imagem'] = None
        tag_imagem = soup.find('img', {'id': 'landingImage'})
        if tag_imagem:
            if 'data-a-dynamic-image' in tag_imagem.attrs:
                dados['url_imagem'] = list(json.loads(tag_imagem.attrs['data-a-dynamic-image']).keys())[0]
            else:
                dados['url_imagem'] = tag_imagem.get('src')
        
        dados['preco_atual_completo'] = None
        seletores_preco_atual = ['#corePrice_feature_div .a-offscreen', '#snsPrice .a-offscreen', '#priceblock_ourprice', '#priceblock_dealprice', '.priceToPay .a-offscreen', '.a-price.a-text-price .a-offscreen']
        for selector in seletores_preco_atual:
            tag = soup.select_one(selector)
            if tag:
                dados['preco_atual_completo'] = tag.get_text(strip=True)
                logger.info(f"Preço atual encontrado com o seletor: {selector}")
                break

        dados['preco_original_completo'] = None
        seletores_preco_original = ['span[data-a-strike="true"] .a-offscreen', '.basisPrice .a-offscreen', '.a-text-strike']
        for selector in seletores_preco_original:
            tag = soup.select_one(selector)
            if tag:
                dados['preco_original_completo'] = tag.get_text(strip=True)
                logger.info(f"Preço original encontrado com o seletor: {selector}")
                break
        
        dados['avaliacao'] = soup.find('span', {'data-hook': 'rating-out-of-text'}).get_text(strip=True) if soup.find('span', {'data-hook': 'rating-out-of-text'}) else 'Sem avaliações'
        dados['num_avaliacoes'] = soup.find('span', {'id': 'acrCustomerReviewText'}).get_text(strip=True) if soup.find('span', {'id': 'acrCustomerReviewText'}) else ''

    except Exception as e:
        logger.error(f"Erro durante a execução do Selenium: {e}")
        dados['erro'] = str(e)
    finally:
        driver.quit()
        logger.info("Navegador Selenium fechado.")
        
    return dados

def gerar_mensagem_divulgacao(dados, link_do_usuario):
    if dados.get('erro'): return f"Erro ao processar: {dados['erro']}"
    if not dados.get('preco_atual_completo'): return f"Produto '{dados.get('titulo', 'Desconhecido')}' parece estar indisponível ou não foi possível obter o preço."
    
    mensagem = f"🔥 OFERTA IMPERDÍVEL 🔥\n\n"
    mensagem += f"🏷️ *Produto:* {dados['titulo']}\n\n"
    if dados.get('preco_original_completo'): mensagem += f"❌ De: ~{dados['preco_original_completo']}~\n"
    mensagem += f"✅ *Por: {dados['preco_atual_completo']}*\n"
    
    if dados.get('preco_original_completo') and dados.get('preco_atual_completo'):
        preco_original_num = limpar_preco(dados['preco_original_completo'])
        preco_atual_num = limpar_preco(dados['preco_atual_completo'])
        if preco_original_num and preco_atual_num and preco_original_num > preco_atual_num:
            desconto = ((preco_original_num - preco_atual_num) / preco_original_num) * 100
            mensagem += f"🤑 *{int(round(desconto, 0))}% de desconto!* 🔥\n"

    mensagem += f"\n⭐ *Avaliação:* {dados['avaliacao']} ({dados['num_avaliacoes']})\n\n"
    mensagem += f"🔗 *Compre aqui com seu desconto:*\n{link_do_usuario}\n\n"
    mensagem += f"🛒 Estoque limitado! Preços podem mudar a qualquer momento."
    return mensagem.strip()

# --- CÉREBRO DO BOT (COM A LÓGICA DE SEGURANÇA) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Olá! Sou seu robô de ofertas. Me envie um link encurtado da Amazon.')

async def processar_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link_encurtado = update.message.text
    if not link_encurtado.startswith('http'):
        await update.message.reply_text('Isso não parece um link válido.')
        return
    
    await update.message.reply_text('Entendido! Processando com o navegador... Isso pode levar até 20 segundos.')
    
    try:
        dados = buscar_dados_produto(link_encurtado)
        mensagem = gerar_mensagem_divulgacao(dados, link_encurtado)
        
        # --- AQUI ESTÁ A NOVA VERIFICAÇÃO DE SEGURANÇA ---
        if "indisponível" in mensagem or "Erro" in mensagem:
            logger.warning(f"Falha ao obter dados. Mensagem de erro: {mensagem}")
            # Avisa APENAS o usuário no chat privado sobre a falha
            await update.message.reply_text(
                f"❌ *Falha ao processar o link.*\n\n"
                f"O robô não conseguiu obter os dados do produto. "
                f"Isso geralmente acontece por um bloqueio temporário da Amazon (CAPTCHA).\n\n"
                f"*Nada foi postado no seu canal.* Tente novamente mais tarde ou com outro link."
            )
            return # Interrompe a execução aqui

        # Se a verificação passar, continua o processo normal
        imagem_path = "imagem_produto.jpg"
        if baixar_imagem(dados.get('url_imagem'), imagem_path):
            with open(imagem_path, 'rb') as foto:
                await context.bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=InputFile(foto), caption=mensagem, parse_mode='Markdown')
        else: # Se falhar o download da imagem, posta só o texto
            await context.bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=mensagem, parse_mode='Markdown')
        
        await update.message.reply_text('✅ Oferta postada com sucesso no seu canal!')

    except Exception as e:
        logger.error(f"Erro inesperado no processamento: {e}")
        await update.message.reply_text(f"Ocorreu um erro geral. Detalhes: {e}")

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
