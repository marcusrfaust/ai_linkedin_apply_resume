#!/usr/bin/env python3
"""
Script de teste para verificar se a busca de vagas está funcionando
"""

import sys
import os
from pathlib import Path

# Adicionar o diretório src ao path
sys.path.append(str(Path(__file__).parent / "src"))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from loguru import logger
from src.aihawk_job_manager import AIHawkJobManager

def setup_driver():
    """Configura o driver do Selenium"""
    chrome_options = Options()
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    driver = webdriver.Chrome(options=chrome_options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    return driver

def test_job_search():
    """Testa a busca de vagas"""
    logger.info("Starting job search test...")
    
    driver = None
    try:
        # Configurar driver
        driver = setup_driver()
        
        # Criar job manager
        job_manager = AIHawkJobManager(driver)
        
        # Configurar parâmetros básicos
        parameters = {
            'company_blacklist': [],
            'title_blacklist': [],
            'positions': ['Software Engineer'],
            'locations': ['Remote'],
            'apply_once_at_company': False,
            'outputFileDirectory': 'data_folder/output'
        }
        
        job_manager.set_parameters(parameters)
        
        # Testar busca
        success = job_manager.test_job_search("Software Engineer", "Remote")
        
        if success:
            logger.info("✓ Test PASSED: Job search is working!")
            return True
        else:
            logger.error("✗ Test FAILED: Job search is not working")
            return False
            
    except Exception as e:
        logger.error(f"✗ Test ERROR: {e}")
        return False
    finally:
        if driver:
            driver.quit()

if __name__ == "__main__":
    success = test_job_search()
    sys.exit(0 if success else 1)
