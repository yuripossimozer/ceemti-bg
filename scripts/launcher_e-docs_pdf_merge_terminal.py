import urllib.request
import sys
import time
import threading

URL = "https://raw.githubusercontent.com/yuripossimozer/ceemti-bg/refs/heads/main/scripts/e-docs_pdf_merge_terminal.py"
nome_arquivo = URL.split('/')[-1]
baixando = True

def animacao_spinner():
    frames = "|/-\\"
    i = 0
    while baixando:
        sys.stdout.write(f'\rDownloading {nome_arquivo} ... {frames[i % 4]}')
        sys.stdout.flush()
        time.sleep(0.1)
        i += 1

if __name__ == "__main__":
    t = threading.Thread(target=animacao_spinner)
    t.start()
    
    try:
        resposta = urllib.request.urlopen(URL, timeout=15)
        codigo = resposta.read().decode('utf-8')
        
        baixando = False
        t.join() 
        
        sys.stdout.write(f'\rDownloading {nome_arquivo} ... done  \n')
        sys.stdout.flush()
        
        exec(codigo, globals())
        
    except Exception as e:
        baixando = False
        t.join()
        
        sys.stdout.write(f'\rDownloading {nome_arquivo} ... error \n')
        sys.stdout.flush()
        
        print(f"Error details: {e}")
        sys.exit(1)
