import json
import os
import random
import time
from itertools import product
from pathlib import Path

from inputimeout import inputimeout, TimeoutOccurred
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

import src.utils as utils
from app_config import MINIMUM_WAIT_TIME, MAXIMUM_WAIT_TIME
from src.job import Job
from src.aihawk_easy_applier import AIHawkEasyApplier
from loguru import logger
import urllib.parse


def limit_wait_time(time_left):
    """Limita o tempo de espera ao MAXIMUM_WAIT_TIME"""
    return min(time_left, MAXIMUM_WAIT_TIME)


def _first(driver, xpaths):
    """Retorna o primeiro WebElement que existir dentre os XPaths fornecidos."""
    for xp in xpaths:
        try:
            el = driver.find_element(By.XPATH, xp)
            if el:
                return el
        except Exception:
            continue
    raise NoSuchElementException("Nenhum dos XPaths correspondeu a um elemento.")


def _all(driver, xpaths):
    """Retorna a primeira lista não vazia dentre os XPaths fornecidos."""
    for xp in xpaths:
        try:
            els = driver.find_elements(By.XPATH, xp)
            if els:
                return els
        except Exception:
            continue
    return []


class EnvironmentKeys:
    def __init__(self):
        logger.debug("Initializing EnvironmentKeys")
        self.skip_apply = self._read_env_key_bool("SKIP_APPLY")
        self.disable_description_filter = self._read_env_key_bool("DISABLE_DESCRIPTION_FILTER")
        logger.debug(f"EnvironmentKeys initialized: skip_apply={self.skip_apply}, disable_description_filter={self.disable_description_filter}")

    @staticmethod
    def _read_env_key(key: str) -> str:
        value = os.getenv(key, "")
        logger.debug(f"Read environment key {key}: {value}")
        return value

    @staticmethod
    def _read_env_key_bool(key: str) -> bool:
        value = os.getenv(key) == "True"
        logger.debug(f"Read environment key {key} as bool: {value}")
        return value


class AIHawkJobManager:
    def __init__(self, driver):
        logger.debug("Initializing AIHawkJobManager")
        self.driver = driver
        self.set_old_answers = set()
        self.easy_applier_component = None
        logger.debug("AIHawkJobManager initialized successfully")

    def set_parameters(self, parameters):
        logger.debug("Setting parameters for AIHawkJobManager")
        self.company_blacklist = parameters.get('company_blacklist', []) or []
        self.title_blacklist = parameters.get('title_blacklist', []) or []
        self.positions = parameters.get('positions', [])
        self.locations = parameters.get('locations', [])
        self.apply_once_at_company = parameters.get('apply_once_at_company', False)
        self.base_search_url = self.get_base_search_url(parameters)
        self.seen_jobs = []

        job_applicants_threshold = parameters.get('job_applicants_threshold', {})
        self.min_applicants = job_applicants_threshold.get('min_applicants', 0)
        self.max_applicants = job_applicants_threshold.get('max_applicants', float('inf'))

        resume_path = parameters.get('uploads', {}).get('resume', None)
        self.resume_path = Path(resume_path) if resume_path and Path(resume_path).exists() else None
        self.output_file_directory = Path(parameters['outputFileDirectory'])
        self.env_config = EnvironmentKeys()
        logger.debug("Parameters set successfully")

    def set_gpt_answerer(self, gpt_answerer):
        logger.debug("Setting GPT answerer")
        self.gpt_answerer = gpt_answerer

    def set_resume_generator_manager(self, resume_generator_manager):
        logger.debug("Setting resume generator manager")
        self.resume_generator_manager = resume_generator_manager

    def _save_debug_snapshot(self, label: str) -> None:
        """Salva page_source e screenshot para depuração."""
        try:
            dbg_dir = self.output_file_directory / "debug"
            dbg_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            html_path = dbg_dir / f"{label}_{ts}.html"
            png_path = dbg_dir / f"{label}_{ts}.png"
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(self.driver.page_source)
            try:
                self.driver.save_screenshot(str(png_path))
            except Exception as e:
                logger.warning(f"Failed to save screenshot: {e}")
            logger.debug(f"Saved debug snapshot: {html_path} and {png_path}")
        except Exception as e:
            logger.warning(f"Failed to save debug snapshot: {e}")

    def start_collecting_data(self):
        searches = list(product(self.positions, self.locations))
        random.shuffle(searches)
        page_sleep = 0
        minimum_time = MINIMUM_WAIT_TIME
        minimum_page_time = time.time() + minimum_time

        for position, location in searches:
            job_page_number = -1
            utils.printyellow(f"Collecting data for {position} in {location}.")
            try:
                while True:
                    page_sleep += 1
                    job_page_number += 1
                    utils.printyellow(f"Going to job page {job_page_number}")
                    self.next_job_page(position, location, job_page_number)
                    time.sleep(random.uniform(0.5, 1.5))
                    utils.printyellow("Starting the collecting process for this page")
                    self.read_jobs()
                    utils.printyellow("Collecting data on this page has been completed!")

                    time_left = minimum_page_time - time.time()
                    if time_left > 0:
                        time_left = limit_wait_time(time_left)
                        utils.printyellow(f"Sleeping for {time_left} seconds.")
                        time.sleep(time_left)
                        minimum_page_time = time.time() + minimum_time
                    if page_sleep % 5 == 0:
                        sleep_time = random.randint(1, 5)
                        utils.printyellow(f"Sleeping for {sleep_time / 60} minutes.")
                        time.sleep(sleep_time)
                        page_sleep += 1
            except Exception:
                pass
            time_left = minimum_page_time - time.time()
            if time_left > 0:
                time_left = limit_wait_time(time_left)
                utils.printyellow(f"Sleeping for {time_left} seconds.")
                time.sleep(time_left)
                minimum_page_time = time.time() + minimum_time
            if page_sleep % 5 == 0:
                sleep_time = random.randint(5, min(15, MAXIMUM_WAIT_TIME))
                utils.printyellow(f"Sleeping for {sleep_time / 60} minutes.")
                time.sleep(sleep_time)
                page_sleep += 1

    def start_applying(self):
        logger.debug("Starting job application process")
        self.easy_applier_component = AIHawkEasyApplier(self.driver, self.resume_path, self.set_old_answers,
                                                          self.gpt_answerer, self.resume_generator_manager)
        searches = list(product(self.positions, self.locations))
        random.shuffle(searches)
        page_sleep = 0
        minimum_time = MINIMUM_WAIT_TIME
        minimum_page_time = time.time() + minimum_time

        for position, location in searches:
            job_page_number = -1
            logger.debug(f"Starting the search for {position} in {location}.")

            try:
                while True:
                    page_sleep += 1
                    job_page_number += 1
                    logger.debug(f"Going to job page {job_page_number}")
                    self.next_job_page(position, location, job_page_number)
                    time.sleep(random.uniform(0.5, 1.5))
                    logger.debug("Starting the application process for this page...")

                    try:
                        jobs = self.get_jobs_from_page()
                        if not jobs:
                            logger.debug("No more jobs found on this page. Exiting loop.")
                            break
                    except Exception as e:
                        logger.error(f"Failed to retrieve jobs: {e}")
                        self._save_debug_snapshot("get_jobs_from_page_error")
                        break

                    try:
                        self.apply_jobs()
                    except Exception as e:
                        logger.error(f"Error during job application: {e}")
                        continue

                    logger.debug("Applying to jobs on this page has been completed!")

                    time_left = minimum_page_time - time.time()
                    time_left = limit_wait_time(time_left)

                    # Ask user if they want to skip waiting, with timeout
                    if time_left > 0:
                        try:
                            user_input = inputimeout(
                                prompt=f"Sleeping for {time_left} seconds. Press 'y' to skip waiting. Timeout 60 seconds : ",
                                timeout=60).strip().lower()
                        except TimeoutOccurred:
                            user_input = ''  # No input after timeout
                        if user_input == 'y':
                            logger.debug("User chose to skip waiting.")
                        else:
                            logger.debug(f"Sleeping for {time_left} seconds as user chose not to skip.")
                            time.sleep(time_left)

                    minimum_page_time = time.time() + minimum_time

                    if page_sleep % 5 == 0:
                        sleep_time = random.randint(5, min(34, MAXIMUM_WAIT_TIME))
                        try:
                            user_input = inputimeout(
                                prompt=f"Sleeping for {sleep_time / 60} minutes. Press 'y' to skip waiting. Timeout 60 seconds : ",
                                timeout=60).strip().lower()
                        except TimeoutOccurred:
                            user_input = ''  # No input after timeout
                        if user_input == 'y':
                            logger.debug("User chose to skip waiting.")
                        else:
                            logger.debug(f"Sleeping for {sleep_time} seconds.")
                            time.sleep(sleep_time)
                        page_sleep += 1
            except Exception as e:
                logger.error(f"Unexpected error during job search: {e}")
                continue

            time_left = minimum_page_time - time.time()
            time_left = limit_wait_time(time_left)

            if time_left > 0:
                try:
                    user_input = inputimeout(
                        prompt=f"Sleeping for {time_left} seconds. Press 'y' to skip waiting. Timeout 60 seconds : ",
                        timeout=60).strip().lower()
                except TimeoutOccurred:
                    user_input = ''  # No input after timeout
                if user_input == 'y':
                    logger.debug("User chose to skip waiting.")
                else:
                    logger.debug(f"Sleeping for {time_left} seconds as user chose not to skip.")
                    time.sleep(time_left)

            minimum_page_time = time.time() + minimum_time

            if page_sleep % 5 == 0:
                sleep_time = random.randint(5, min(15, MAXIMUM_WAIT_TIME))
                try:
                    user_input = inputimeout(
                        prompt=f"Sleeping for {sleep_time / 60} minutes. Press 'y' to skip waiting: ",
                        timeout=60).strip().lower()
                except TimeoutOccurred:
                    user_input = ''  # No input after timeout
                if user_input == 'y':
                    logger.debug("User chose to skip waiting.")
                else:
                    logger.debug(f"Sleeping for {sleep_time} seconds.")
                    time.sleep(sleep_time)
                page_sleep += 1

    def get_jobs_from_page(self):
        try:
            no_jobs_element = _first(self.driver, [
                '//*[@class="jobs-search-two-pane__no-results-banner--expand"]',
                '//*[contains(@class,"no-results-banner")]'
            ])
            if no_jobs_element and ('No matching jobs found' in no_jobs_element.text or 'unfortunately, things aren' in self.driver.page_source.lower()):
                logger.debug("No matching jobs found on this page, skipping.")
                return []
        except Exception:
            pass

        try:
            # Seletores atualizados para LinkedIn 2025
            list_container = _first(self.driver, [
                '//ul[@data-test-reusables-search__results-list]',
                '//div[contains(@class,"scaffold-layout__list-container")]',
                '//*[@class="jobs-search-results-list"]//ul',
                '//*[@class="jobs-search-results-list"]',
                '//div[contains(@class,"jobs-search-results")]//ul',
                '//main[@id="main"]//ul'
            ])
            utils.scroll_slow(self.driver, list_container)
            utils.scroll_slow(self.driver, list_container, step=300, reverse=True)

            job_list_elements = _all(self.driver, [
                # Seletores LinkedIn 2025 - baseados na estrutura real encontrada
                '//div[contains(@class, "job-card-container")]', # Seletor principal encontrado
                '//div[@data-job-id]', # Seletor por data-job-id
                '//div[contains(@class, "job-card-list")]', # Seletor alternativo
                '//div[contains(@class, "jobs-search-results-list__list-item")]', # Seletor de lista
                '//ul[@data-test-reusables-search__results-list]/li[contains(@class,"jobs-search-results__list-item")]',
                '//*[@class="scaffold-layout__list-container"]//li[contains(@class,"jobs-search-results__list-item")]',
                '//div[contains(@class,"base-card")]',
                '//li[contains(@class,"result-card")]',
                '//article[contains(@class,"job-")]',
                '//div[contains(@data-entity-urn,"urn:li:jobPosting")]',
                # Novos seletores para LinkedIn 2025
                '//div[contains(@class,"jobs-search-results-list")]//li',
                '//div[contains(@class,"scaffold-layout__list")]//li',
                '//main//ul//li[contains(@class,"job")]',
                '//div[@id="main"]//ul//li',
                '//div[contains(@class,"jobs-search-results")]//div[contains(@class,"job-card")]'
            ])
            if not job_list_elements:
                logger.debug("No job class elements found on page, skipping.")
                self._save_debug_snapshot("no_job_items")
                return []

            return job_list_elements

        except NoSuchElementException:
            logger.debug("No job results found on the page.")
            self._save_debug_snapshot("no_results_section")
            return []
        except Exception as e:
            logger.error(f"Error while fetching job elements: {e}")
            self._save_debug_snapshot("get_jobs_generic_error")
            return []

    def read_jobs(self):
        try:
            no_jobs_element = _first(self.driver, [
                '//*[@class="jobs-search-two-pane__no-results-banner--expand"]',
                '//*[contains(@class,"no-results-banner")]'
            ])
            if no_jobs_element and ('No matching jobs found' in no_jobs_element.text or 'unfortunately, things aren' in self.driver.page_source.lower()):
                raise Exception("No more jobs on this page")
        except Exception:
            pass

        list_container = _first(self.driver, [
            '//ul[@data-test-reusables-search__results-list]',
            '//*[@class="jobs-search-results-list"]//ul',
            '//*[@class="jobs-search-results-list"]'
        ])
        utils.scroll_slow(self.driver, list_container)
        utils.scroll_slow(self.driver, list_container, step=300, reverse=True)
        job_list_elements = _all(self.driver, [
            # Seletores LinkedIn 2025 - baseados na estrutura real encontrada
            '//div[contains(@class, "job-card-container")]', # Seletor principal encontrado
            '//div[@data-job-id]', # Seletor por data-job-id
            '//div[contains(@class, "job-card-list")]', # Seletor alternativo
            '//div[contains(@class, "jobs-search-results-list__list-item")]', # Seletor de lista
            '//ul[@data-test-reusables-search__results-list]/li[contains(@class,"jobs-search-results__list-item")]',
            '//*[@class="scaffold-layout__list-container"]//li[contains(@class,"jobs-search-results__list-item")]'
        ])
        if not job_list_elements:
            self._save_debug_snapshot("read_jobs_no_items")
            raise Exception("No job class elements found on page")
        job_list = [Job(*self.extract_job_information_from_tile(job_element)) for job_element in job_list_elements]
        for job in job_list:
            if self.is_blacklisted(job.title, job.company, job.link):
                utils.printyellow(f"Blacklisted {job.title} at {job.company}, skipping...")
                self.write_to_file(job, "skipped")
                continue
            try:
                self.write_to_file(job,'data')
            except Exception as e:
                self.write_to_file(job, "failed")
                continue

    def apply_jobs(self):
        try:
            no_jobs_element = _first(self.driver, [
                '//*[@class="jobs-search-two-pane__no-results-banner--expand"]',
                '//*[contains(@class,"no-results-banner")]'
            ])
            if no_jobs_element and ('No matching jobs found' in no_jobs_element.text or 'unfortunately, things aren' in self.driver.page_source.lower()):
                logger.debug("No matching jobs found on this page, skipping")
                return
        except Exception:
            pass

        job_list_elements = _all(self.driver, [
            # Seletores LinkedIn 2025 - baseados na estrutura real encontrada
            '//div[contains(@class, "job-card-container")]', # Seletor principal encontrado
            '//div[@data-job-id]', # Seletor por data-job-id
            '//div[contains(@class, "job-card-list")]', # Seletor alternativo
            '//div[contains(@class, "jobs-search-results-list__list-item")]', # Seletor de lista
            '//ul[@data-test-reusables-search__results-list]/li[contains(@class,"jobs-search-results__list-item")]',
            '//*[@class="scaffold-layout__list-container"]//li[contains(@class,"jobs-search-results__list-item")]'
        ])

        if not job_list_elements:
            logger.debug("No job class elements found on page, skipping")
            self._save_debug_snapshot("apply_no_items")
            return

        job_list = [Job(*self.extract_job_information_from_tile(job_list_element)) for job_list_element in job_list_elements]

        # Filtrar vagas inválidas (sem título/empresa/link ou link malformado)
        job_list = [j for j in job_list if getattr(j, 'title', '').strip() and getattr(j, 'company', '').strip() and getattr(j, 'link', '').startswith('http')]

        for job in job_list:

            logger.debug(f"Starting applicant for job: {job.title} at {job.company}")
            
            if self.is_blacklisted(job.title, job.company, job.link):
                logger.debug(f"Job blacklisted: {job.title} at {job.company}")
                self.write_to_file(job, "skipped")
                continue
            if self.is_already_applied_to_job(job.title, job.company, job.link):
                self.write_to_file(job, "skipped")
                continue
            if self.is_already_applied_to_company(job.company):
                self.write_to_file(job, "skipped")
                continue
            try:
                # Tentar aplicar se não for "Applied" ou se o método não foi detectado (string vazia)
                if job.apply_method not in {"Applied"} and job.apply_method.strip():
                    logger.debug(f"Attempting to apply to job: {job.title} at {job.company} (method: {job.apply_method})")
                    self.easy_applier_component.job_apply(job)
                    self.write_to_file(job, "success")
                    logger.debug(f"Applied to job: {job.title} at {job.company}")
                elif not job.apply_method.strip():
                    # Se não conseguiu detectar o método, tentar aplicar mesmo assim
                    logger.debug(f"Apply method not detected, attempting to apply anyway: {job.title} at {job.company}")
                    self.easy_applier_component.job_apply(job)
                    self.write_to_file(job, "success")
                    logger.debug(f"Applied to job: {job.title} at {job.company}")
                else:
                    logger.debug(f"Skipping job (already applied): {job.title} at {job.company}")
            except Exception as e:
                logger.error(f"Failed to apply for {job.title} at {job.company}: {e}")
                # Tentativa leve de recuperação para sessão inválida
                if "invalid session id" in str(e).lower():
                    try:
                        logger.warning("WebDriver session appears invalid. Attempting light recovery (reload jobs page).")
                        current_url = self.driver.current_url
                        # Recarrega a página atual de busca se possível
                        if current_url:
                            self.driver.get(current_url)
                        else:
                            # Fallback para página de empregos
                            self.driver.get("https://www.linkedin.com/jobs/")
                        time.sleep(2)
                    except Exception as rec_err:
                        logger.warning(f"Recovery attempt failed: {rec_err}")
                self._save_debug_snapshot("apply_fail")
                self.write_to_file(job, "failed")
                continue

    def write_to_file(self, job, file_name):
        logger.debug(f"Writing job application result to file: {file_name}")
        # Evitar erro quando pdf_path está vazio
        pdf_uri = ""
        try:
            if getattr(job, 'pdf_path', '').strip():
                pdf_path = Path(job.pdf_path).resolve()
                pdf_uri = pdf_path.as_uri()
        except Exception:
            pdf_uri = ""
        data = {
            "company": job.company,
            "job_title": job.title,
            "link": job.link,
            "job_recruiter": job.recruiter_link,
            "job_location": job.location,
            "pdf_path": pdf_uri
        }
        file_path = self.output_file_directory / f"{file_name}.json"
        if not file_path.exists():
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump([data], f, indent=4)
                logger.debug(f"Job data written to new file: {file_name}")
        else:
            with open(file_path, 'r+', encoding='utf-8') as f:
                try:
                    existing_data = json.load(f)
                except json.JSONDecodeError:
                    logger.error(f"JSON decode error in file: {file_path}")
                    existing_data = []
                existing_data.append(data)
                f.seek(0)
                json.dump(existing_data, f, indent=4)
                f.truncate()
                logger.debug(f"Job data appended to existing file: {file_name}")

    def get_base_search_url(self, parameters):
        logger.debug("Constructing base search URL")
        url_parts = []
        if parameters['remote']:
            url_parts.append("f_CF=f_WRA")
        experience_levels = [str(i + 1) for i, (level, v) in enumerate(parameters.get('experience_level', {}).items()) if
                             v]
        if experience_levels:
            url_parts.append(f"f_E={','.join(experience_levels)}")
        url_parts.append(f"distance={parameters['distance']}")
        job_types = [key[0].upper() for key, value in parameters.get('jobTypes', {}).items() if value]
        if job_types:
            url_parts.append(f"f_JT={','.join(job_types)}")
        date_mapping = {
            "all time": "",
            "month": "&f_TPR=r2592000",
            "week": "&f_TPR=r604800",
            "24 hours": "&f_TPR=r86400"
        }
        date_param = next((v for k, v in date_mapping.items() if parameters.get('date', {}).get(k)), "")
        url_parts.append("f_LF=f_AL")  # Easy Apply
        base_url = "&".join(url_parts)
        full_url = f"?{base_url}{date_param}"
        logger.debug(f"Base search URL constructed: {full_url}")
        return full_url

    def next_job_page(self, position, location, job_page):
        logger.debug(f"Navigating to next job page: {position} in {location}, page {job_page}")
        encoded_position = urllib.parse.quote(position)
        
        # Simplificar URL para teste inicial - usar parâmetros básicos
        if job_page == 0:
            # URL simples para primeira página
            full_url = f"https://www.linkedin.com/jobs/search/?keywords={encoded_position}&location={location}&f_AL=true"
        else:
            # URL com paginação
            full_url = f"https://www.linkedin.com/jobs/search/?keywords={encoded_position}&location={location}&f_AL=true&start={job_page * 25}"
        
        logger.debug(f"Navigating to simplified URL: {full_url}")
        self.driver.get(full_url)
        
        # Aguardar carregamento da página
        time.sleep(1)
        
        # Verificar se a página carregou corretamente
        current_url = self.driver.current_url
        logger.debug(f"Current URL after navigation: {current_url}")
        
        # Salvar snapshot para debug
        self._save_debug_snapshot(f"page_{job_page}_loaded")

    def extract_job_information_from_tile(self, job_tile):
        logger.debug("Extracting job information from tile")
        job_title, company, job_location, apply_method, link = "", "", "", "", ""
        try:
            # Seletores atualizados para título da vaga 2025 - baseados na estrutura real
            title_link_el = _first(job_tile, [
                './/a[contains(@class, "job-card-container__link")]',
                './/a[contains(@class, "job-card-list__title--link")]',
                './/a[contains(@href, "/jobs/view/")]',
                './/a[contains(@class, "artdeco-entity-lockup__title")]//a',
                './/a[contains(@class, "job-card-list__title")]',
                './/a[contains(@data-tracking-control-name, "job_card")]',
                './/h3//a[contains(@href, "/jobs/view/")]'
            ])
            try:
                strong = title_link_el.find_element(By.TAG_NAME, 'strong')
                job_title = strong.text or title_link_el.text
            except Exception:
                job_title = title_link_el.text
            href = title_link_el.get_attribute('href') or ''
            link = href.split('?')[0] if href else ''
        except Exception as e:
            logger.warning(f"Failed to extract title/link: {e}")

        try:
            # Seletores atualizados para empresa 2025 - baseados na estrutura real
            company_el = _first(job_tile, [
                './/span[contains(@class, "artdeco-entity-lockup__subtitle")]',
                './/div[contains(@class, "artdeco-entity-lockup__subtitle")]//span',
                './/*[contains(@class, "job-card-container__primary-description")]',
                './/span[contains(@class, "job-card-container__primary-description")]',
                './/a[contains(@href, "/company/") and @data-field="companyName"]',
                './/h4[contains(@class, "base-card__subtitle")]//a'
            ])
            company = company_el.text
        except Exception:
            logger.warning("Company not found.")

        try:
            # Seletores atualizados para localização 2025 - baseados na estrutura real
            loc_el = _first(job_tile, [
                './/ul[contains(@class, "job-card-container__metadata-wrapper")]//li[1]//span',
                './/div[contains(@class, "artdeco-entity-lockup__caption")]//span',
                './/*[contains(@class, "job-card-container__metadata-item")]',
                './/*[@data-test="job-card-location"]',
                './/div[contains(@class, "base-card__metadata")]',
                './/span[contains(@class, "job-card-container__metadata-wrapper")]//span[2]'
            ])
            job_location = loc_el.text
        except Exception:
            logger.warning("Job location not found.")

        try:
            # Seletores atualizados para método de aplicação 2025 - baseados na estrutura real
            method_el = _first(job_tile, [
                './/ul[contains(@class, "job-card-container__metadata-wrapper")]//li[contains(., "Easy Apply")]//span',
                './/span[contains(text(), "Easy Apply")]',
                './/button[contains(@class, "jobs-apply-button")]//span[contains(@class, "artdeco-button__text")]',
                './/button[contains(@aria-label, "Easy Apply")]//span',
                './/*[contains(@class, "job-card-container__apply-method")]//span',
                './/span[contains(., "Easy Apply")]/ancestor::button[1]'
            ])
            apply_method = method_el.text
        except Exception:
            apply_method = "Easy Apply"  # Tentar aplicar quando não conseguir detectar
            logger.warning("Apply method not found, assuming 'Easy Apply' to attempt application.")

        return job_title, company, job_location, link, apply_method

    def is_blacklisted(self, job_title, company, link):
        logger.debug(f"Checking if job is blacklisted: {job_title} at {company}")
        job_title_words = job_title.lower().split(' ')
        title_blacklisted = any(word in job_title_words for word in map(str.lower, self.title_blacklist))
        company_blacklisted = company.strip().lower() in (word.strip().lower() for word in self.company_blacklist)
        link_seen = link in self.seen_jobs
        is_blacklisted = title_blacklisted or company_blacklisted or link_seen
        logger.debug(f"Job blacklisted status: {is_blacklisted}")

        return title_blacklisted or company_blacklisted or link_seen

    def is_already_applied_to_job(self, job_title, company, link):
        link_seen = link in self.seen_jobs
        if link_seen:
            logger.debug(f"Already applied to job: {job_title} at {company}, skipping...")
        return link_seen

    def is_already_applied_to_company(self, company):
        if not self.apply_once_at_company:
            return False

        output_files = ["success.json"]
        for file_name in output_files:
            file_path = self.output_file_directory / file_name
            if file_path.exists():
                with open(file_path, 'r', encoding='utf-8') as f:
                    try:
                        existing_data = json.load(f)
                        for applied_job in existing_data:
                            if applied_job['company'].strip().lower() == company.strip().lower():
                                logger.debug(
                                    f"Already applied at {company} (once per company policy), skipping...")
                                return True
                    except json.JSONDecodeError:
                        continue
        return False

    def test_job_search(self, position="Software Engineer", location="Remote"):
        """Método de teste para verificar se a busca de vagas está funcionando"""
        logger.info(f"Testing job search for {position} in {location}")
        
        try:
            # Navegar para a página de busca
            self.next_job_page(position, location, 0)
            
            # Aguardar carregamento
            time.sleep(2)
            
            # Verificar se encontrou vagas
            jobs = self.get_jobs_from_page()
            
            if jobs:
                logger.info(f"✓ SUCCESS: Found {len(jobs)} jobs on the page!")
                return True
            else:
                logger.warning("✗ FAILED: No jobs found on the page")
                return False
                
        except Exception as e:
            logger.error(f"✗ ERROR during test: {e}")
            return False

