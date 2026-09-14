import urllib.request
import sys
import time
import threading
import traceback

URL_SCRIPT = "https://raw.githubusercontent.com/yuripossimozer/ceemti-bg/refs/heads/main/scripts/e-docs_pdf_merge_terminal.py"
nome_arquivo = URL_SCRIPT.split('/')[-1]
baixando = True

def animacao_spinner():
    frames = "|/—\\"
    i = 0
    while baixando:
        sys.stdout.write(f'\rDownloading {nome_arquivo} ... {frames[i % 4]}')
        sys.stdout.flush()
        time.sleep(0.2)
        i += 1

if __name__ == "__main__":
    t = threading.Thread(target=animacao_spinner)
    t.start()
    
    try:
        resposta = urllib.request.urlopen(URL_SCRIPT, timeout=15)
        codigo = resposta.read().decode('utf-8')
        
        baixando = False
        t.join() 
        
        sys.stdout.write(f'\rDownloading {nome_arquivo} ... done  \n')
        sys.stdout.flush()
        
    except Exception as e:
        baixando = False
        t.join()
        sys.stdout.write(f'\rDownloading {nome_arquivo} ... error \n')
        sys.stdout.flush()
        print(f"Falha na rede: {e}")
        input("\nPressione Enter para fechar...")
        sys.exit(1)

    try:
        exec(codigo, globals())
        
    except BaseException as e:
        print("\n--- OCORREU UM ERRO NA EXECUÇÃO ---")
        traceback.print_exc() 
        input("\nPressione Enter para fechar...")
        sys.exit(1)
