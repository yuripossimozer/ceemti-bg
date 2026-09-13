import urllib.request
import sys

URL_DO_SCRIPT = "https://raw.githubusercontent.com/yuripossimozer/ceemti-bg/refs/heads/main/scripts/e-docs_pdf_merge_terminal.py"

def iniciar_aplicacao():
    print("Baixando a versão mais recente do script...")
    try:
        # Baixa o conteúdo do script com timeout de 15 segundos
        resposta = urllib.request.urlopen(URL_DO_SCRIPT, timeout=15)
        codigo_fonte = resposta.read().decode('utf-8')
        
        # Executa o código baixado diretamente na memória
        exec(codigo_fonte, globals())
        
    except urllib.error.URLError as e:
        print(f"Erro de conexão. Verifique sua internet: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Erro durante a execução do script: {e}")
        sys.exit(1)

if __name__ == "__main__":
    iniciar_aplicacao()
