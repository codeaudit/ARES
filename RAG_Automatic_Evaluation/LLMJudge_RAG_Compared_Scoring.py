
from cProfile import label
import torch.nn as nn
from transformers import T5Tokenizer, T5EncoderModel, T5ForConditionalGeneration
from transformers import BertModel, AutoTokenizer, AutoModel, GPT2Tokenizer
#import tensorflow as tf

import pandas as pd
import numpy as np
import ast
import datasets
from datasets import load_metric
from transformers import TrainingArguments, Trainer

import pyarrow as pa
import pyarrow.dataset as ds

from torch.optim import Adam
from torch.utils.data import DataLoader
from transformers import get_scheduler, AutoModelForCausalLM, AutoConfig, AutoModelForSequenceClassification #MptForSequenceClassification

import torch
from tqdm.auto import tqdm
import statistics
import time

import subprocess as sp
import os
from sklearn.model_selection import train_test_split
import json
import random
import re
import scipy.stats as stats
import argparse

from ppi import clt_iid, binomial_iid, pp_mean_iid_asymptotic
from Evaluation_Functions import calculate_accuracy, few_shot_context_relevance_scoring
from Evaluation_Functions import few_shot_answer_faithfulness_scoring, few_shot_answer_relevance_scoring

#############################################################

random_state = 42

np.random.seed(random_state)
random.seed(random_state)
torch.manual_seed(random_state)
os.environ['PYTHONHASHSEED'] = str(random_state)

############################################################

class CustomBERTModel(nn.Module):
    def __init__(self, number_of_labels, model_choice):

          super(CustomBERTModel, self).__init__()
          if model_choice in ["mosaicml/mpt-7b-instruct", "mosaicml/mpt-7b"]:

            config = AutoConfig.from_pretrained(model_choice, trust_remote_code=True)
            config.attn_config['attn_impl'] = 'triton'  # change this to use triton-based FlashAttention
            config.max_seq_len = max_token_length

            model_encoding = AutoModelForCausalLM.from_pretrained(
                model_choice,
                config=config,
                #max_seq_len=max_token_length,
                torch_dtype=torch.bfloat16,
                trust_remote_code=True,
                use_auth_token=True
            )
            embedding_size = 4096
            self.encoderModel = model_encoding.transformer

          elif model_choice in ['mosaicml/mpt-1b-redpajama-200b']:

            #model_encoding = AutoModelForCausalLM.from_pretrained("mosaicml/mpt-1b-redpajama-200b", trust_remote_code=True, 
            #                                                      torch_dtype=torch.bfloat16) #attn_impl='triton', 
            model_encoding = MptForSequenceClassification.from_pretrained("mosaicml/mpt-1b-redpajama-200b", trust_remote_code=True)
            embedding_size = 2048
            self.encoderModel = model_encoding
          
          elif model_choice in ["google/t5-large-lm-adapt", "google/t5-xl-lm-adapt"]:

            model_encoding = AutoModelForSequenceClassification.from_pretrained(model_choice)
            #model_encoding = AutoModel.from_pretrained(model_choice, torch_dtype=torch.bfloat16)
            embedding_size = 1024
            self.encoderModel = model_encoding#.transformer

          elif model_choice in ["roberta-large", "microsoft/deberta-v3-large"]:

            model_encoding = AutoModel.from_pretrained(model_choice)
            embedding_size = 1024
            self.encoderModel = model_encoding

          elif model_choice in ["microsoft/deberta-v2-xlarge", "microsoft/deberta-v2-xxlarge"]:

            model_encoding = AutoModel.from_pretrained(model_choice)
            embedding_size = 1536
            self.encoderModel = model_encoding
          
          else:

            model_encoding = AutoModel.from_pretrained(model_choice)
            embedding_size = 768
            self.encoderModel = model_encoding

          #########################################

          self.classifier = nn.Sequential(nn.Linear(embedding_size, 256), nn.Linear(256, number_of_labels))
          self.embedding_size = embedding_size


    def forward(self, ids, mask, labels=None, decoder_input_ids=None):
          
        if model_choice in ["t5-small", "google/t5-xl-lm-adapt", "google/t5-large-lm-adapt", "mosaicml/mpt-1b-redpajama-200b"]:
            total_output = self.encoderModel(input_ids=ids, attention_mask=mask) #labels=labels
            return total_output['logits']
        else:
            total_output = self.encoderModel(ids, attention_mask=mask)
            sequence_output = total_output['last_hidden_state']

            last_hidden_state_formatted = sequence_output[:,0,:].view(-1, self.embedding_size)
            linear2_output = self.classifier(last_hidden_state_formatted)

            return linear2_output

############################################################

def combine_query_document(query: str, document: str, answer=None):
    cleaned_document = re.sub(r'\n+', '\n', document.replace("\r"," ").replace("\t"," ")).strip()
    cleaned_document = cleaned_document.replace("=", " ").replace("-", " ")
    cleaned_document = re.sub(r'\s+', ' ', cleaned_document).strip()
    cleaned_document = (" ").join(cleaned_document.split(" ")[:512])

    if len(query.split(" ")) > 100:
        query = (" ").join(query.split(" ")[:30])

    if answer is None:
        return query + " | " + cleaned_document
    else:
        try:
            return query + " | " + cleaned_document + " | " + answer
        except:
            print("Error with combine_query_document")
            print("Query: " + str(query))
            print("Cleaned Document: " + str(cleaned_document))
            print("Answer: " + str(answer))
            #return str(query) + " | " + str(cleaned_document) + " | " + str(answer)
            return "Error"

def tokenize_function(examples):

    return tokenizer(examples["text"], padding="max_length", truncation=True)#.input_ids

############################################################

def prepare_dataset_for_evaluation(dataframe, label_column: str, text_column: str):
    test_set_text = [dataframe.iloc[i][text_column] for i in range(len(dataframe))]
    test_set_label = [dataframe.iloc[i][label_column] for i in range(len(dataframe))]

    test_dataset_pandas = pd.DataFrame({'label': test_set_label, 'text': test_set_text})
    test_dataset_arrow = pa.Table.from_pandas(test_dataset_pandas)
    test_dataset_arrow = datasets.Dataset(test_dataset_arrow)

    classification_dataset = datasets.DatasetDict({'test' : test_dataset_arrow})
    tokenized_datasets = classification_dataset.map(tokenize_function, batched=True)

    tokenized_datasets = tokenized_datasets.remove_columns(["text"])
    tokenized_datasets = tokenized_datasets.rename_column("label", "labels")
    tokenized_datasets.set_format("torch")

    eval_dataloader = DataLoader(tokenized_datasets['test'], batch_size=assigned_batch_size)
    return eval_dataloader

############################################################

def calculate_ppi(Y_labeled,  Yhat_labeled, Yhat_unlabeled, alpha, num_trials):

    n_max = Y_labeled.shape[0]
    ns = np.linspace(100,n_max,20).astype(int)

    # Imputed-only estimate
    imputed_estimate = (Yhat_labeled.sum() + Yhat_unlabeled.sum())/(Yhat_labeled.shape[0] + Yhat_unlabeled.shape[0])

    # Run prediction-powered inference and classical inference for many values of n
    ci = np.zeros((num_trials, ns.shape[0], 2))
    ci_classical = np.zeros((num_trials, ns.shape[0], 2))
    for i in tqdm(range(ns.shape[0])):
        for j in range(num_trials):
            # Prediction-Powered Inference
            n = ns[i]
            rand_idx = np.random.permutation(n)
            f = Yhat_labeled.astype(float)[rand_idx[:n]]
            y = Y_labeled.astype(float)[rand_idx[:n]]    
            output = pp_mean_iid_asymptotic(y,f,Yhat_unlabeled,alpha)
            ci[j,i,:] = output
            # Classical interval
            ci_classical[j,i,:] = binomial_iid(n,alpha,y.mean())
 
    ci_imputed = binomial_iid(Yhat_unlabeled.shape[0], alpha, imputed_estimate)
    avg_ci = ci.mean(axis=0)[-1]
    avg_ci_classical = ci_classical.mean(axis=0)[-1]
    return avg_ci, avg_ci_classical, ci_imputed

######################################################################







######################################################################

if __name__ == '__main__':

    """ parser = argparse.ArgumentParser()

    parser.add_argument("--alpha", type=float, required=True)
    parser.add_argument("--num_trials", type=int, required=True)
    parser.add_argument("--evaluation_datasets", type=list, required=True)
    parser.add_argument("--checkpoints", type=list, required=True)
    parser.add_argument("--labels", type=list, required=True)

    parser.add_argument("--GPT_scoring", type=bool, default=False, required=True)
    parser.add_argument("--gpt_model", type=str, default="gpt-3.5-turbo-16k", required=False)
    parser.add_argument("--perform_zero_shot", type=bool, default=False, required=False)
    parser.add_argument("--few_shot_examples_filepath", type=str, required=False)

    parser.add_argument("--Y_labeled_count", type=list, default=300, required=False)
    parser.add_argument("--use_pseudo_human_labels", type=bool, default=False, required=False)
    parser.add_argument("--gold_label_path", type=str, required=False)

    args = parser.parse_rgs()

    alpha = args.alpha
    num_trials = args.num_trials
    evaluation_datasets = args.evaluation_datasets
    checkpoints = args.checkpoints
    labels = args.labels
    
    GPT_scoring = args.GPT_scoring
    gpt_model = args.gpt_model
    perform_zero_shot = args.perform_zero_shot
    few_shot_examples_filepath = args.few_shot_examples_filepath

    Y_labeled_count = args.Y_labeled_count
    use_pseudo_human_labels = args.use_pseudo_human_labels
    gold_label_path = args.gold_label_path """

    ###############

    ### Instructions

    # Settings for Human-labeled gold set for PPI
    alpha = 0.05 #0.05
    num_trials = 1000
    Y_labeled_count = 300
    #evaluation_datasets = ['../datasets_v2/nq/ratio_0.7_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.725_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.75_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.775_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.8_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.825_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.85_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.875_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.9_reformatted_full_articles_False_validation_with_negatives.tsv']
    #evaluation_datasets = ['../datasets_v2/nq/ratio_0.5_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.525_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.55_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.575_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.6_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.625_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.65_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.675_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/nq/ratio_0.7_reformatted_full_articles_False_validation_with_negatives.tsv']
    #evaluation_datasets = ['../datasets_v2/hotpotqa/ratio_0.7_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/hotpotqa/ratio_0.725_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/hotpotqa/ratio_0.75_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/hotpotqa/ratio_0.775_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/hotpotqa/ratio_0.8_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/hotpotqa/ratio_0.825_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/hotpotqa/ratio_0.85_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/hotpotqa/ratio_0.875_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/hotpotqa/ratio_0.9_reformatted_full_articles_False_validation_with_negatives.tsv']
    #evaluation_datasets = ['../datasets_v2/wow/ratio_0.7_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/wow/ratio_0.725_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/wow/ratio_0.75_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/wow/ratio_0.775_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/wow/ratio_0.8_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/wow/ratio_0.825_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/wow/ratio_0.85_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/wow/ratio_0.875_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/wow/ratio_0.9_reformatted_full_articles_False_validation_with_negatives.tsv']
    #evaluation_datasets = ['../datasets_v2/fever/ratio_0.7_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/fever/ratio_0.725_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/fever/ratio_0.75_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/fever/ratio_0.775_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/fever/ratio_0.8_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/fever/ratio_0.825_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/fever/ratio_0.85_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/fever/ratio_0.875_reformatted_full_articles_False_validation_with_negatives.tsv', '../datasets_v2/fever/ratio_0.9_reformatted_full_articles_False_validation_with_negatives.tsv']
    #evaluation_datasets = ['../datasets_v2/multirc/ratio_0.7_validation_with_negatives.tsv', '../datasets_v2/multirc/ratio_0.725_validation_with_negatives.tsv', '../datasets_v2/multirc/ratio_0.75_validation_with_negatives.tsv', '../datasets_v2/multirc/ratio_0.775_validation_with_negatives.tsv', '../datasets_v2/multirc/ratio_0.8_validation_with_negatives.tsv', '../datasets_v2/multirc/ratio_0.825_validation_with_negatives.tsv', '../datasets_v2/multirc/ratio_0.85_validation_with_negatives.tsv', '../datasets_v2/multirc/ratio_0.875_validation_with_negatives.tsv', '../datasets_v2/multirc/ratio_0.9_validation_with_negatives.tsv']
    evaluation_datasets = ['../datasets_v2/record/ratio_0.7_validation_with_negatives.tsv', '../datasets_v2/record/ratio_0.725_validation_with_negatives.tsv', '../datasets_v2/record/ratio_0.75_validation_with_negatives.tsv', '../datasets_v2/record/ratio_0.775_validation_with_negatives.tsv', '../datasets_v2/record/ratio_0.8_validation_with_negatives.tsv', '../datasets_v2/record/ratio_0.825_validation_with_negatives.tsv', '../datasets_v2/record/ratio_0.85_validation_with_negatives.tsv', '../datasets_v2/record/ratio_0.875_validation_with_negatives.tsv', '../datasets_v2/record/ratio_0.9_validation_with_negatives.tsv']
    correct_ranking = [i for i in range(0, len(evaluation_datasets))]
    use_pseudo_human_labels = True

    # Settings for fine-tuned LLM-as-a-Judge scoring
    #checkpoints = ["",""]
    #checkpoints = ["../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-nq_synthetic_queries_v4.tsv/5e-06_0_False_1True_False_Context_Relevance_Label.pt",
    #               "../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-nq_synthetic_queries_v4.1.tsv/5e-06_1_True_Answer_Faithfulness_Label_nq_reformatted_validation_with_negatives_1867825.pt"]
    #checkpoints = ["../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-hotpotqa_synthetic_queries_v1.tsv/5e-06_0_False_1True_False.pt",
    #               "../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-hotpotqa_synthetic_queries_v1.tsv/5e-06_0_False_1True_False_Answer_Faithfulness_Label.pt"]
    #checkpoints = ["../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-hotpotqa_synthetic_queries_v2.tsv/5e-06_0_False_1True_False_Context_Relevance_Label.pt",
    #               ""]
    #checkpoints = ["../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-wow_synthetic_queries_v1.tsv/5e-06_0_False_1True_False_Context_Relevance_Label.pt",
    #               "../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-wow_synthetic_queries_v1.1.tsv/5e-06_1_True_Answer_Faithfulness_Label_wow_reformatted_validation_with_negatives_1867825.pt"]
    #checkpoints = ["../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-fever_synthetic_queries_v3.1.tsv/5e-06_0_False_1True_False_Context_Relevance_Label.pt",
    #               "../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-fever_synthetic_queries_v3.2.tsv/5e-06_1_True_Answer_Faithfulness_Label_fever_reformatted_validation_with_negatives_1867825.pt"]
    #checkpoints = ["../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-multirc_synthetic_queries_v1.tsv/5e-06_0_False_1True_False_Context_Relevance_Label.pt",
    #               "../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-multirc_synthetic_queries_v1.tsv/5e-06_1_True_Answer_Faithfulness_Label.pt"]
    checkpoints = ["../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-record_synthetic_queries_v1.tsv/5e-06_1_True_Context_Relevance_Label_record_validation_with_negatives_1867825.pt",
                   "../LLM-as-a-Judge_Adaptation/checkpoints/microsoft-deberta-v3-large/datasets-record_synthetic_queries_v1.tsv/5e-06_1_True_Answer_Faithfulness_Label_record_validation_with_negatives_1867825.pt"]

    labels = ['Context_Relevance_Label', 'Answer_Faithfulness_Label']
    #labels = ['Answer_Faithfulness_Label']
    assigned_batch_size = 1
    number_of_labels = 2

    # Settings for zero/few-shot GPT scoring
    GPT_scoring = False
    gpt_model = "gpt-3.5-turbo-16k"
    perform_zero_shot = False
    #few_shot_examples_filepath = "../datasets_v2/HotPotQA_Few_shot_prompt_v1.tsv"
    #few_shot_examples_filepath = "../datasets_v2/WoW_Few_shot_prompt_v1.tsv"
    #few_shot_examples_filepath = "../datasets_v2/FEVER_Few_shot_prompt_v1.tsv"
    #few_shot_examples_filepath = "../datasets_v2/NQ_Few_shot_prompt_v1.tsv"
    #few_shot_examples_filepath = "../datasets_v2/MultiRC_Few_shot_prompt_v1.tsv"
    few_shot_examples_filepath = "../datasets_v2/ReCoRD_Few_shot_prompt_v1.tsv"

    ############################################################









    ######################################################################

    if GPT_scoring:
        checkpoint = ["" for _ in range(len(labels))]

    few_shot_examples = pd.read_csv(few_shot_examples_filepath, sep="\t")
    print("few_shot_examples")
    print(len(few_shot_examples))
    print(few_shot_examples.head())

    ####################################################################

    def clean_document(document: str):
        cleaned_document = re.sub(r'\n+', '\n', document.replace("\r"," ").replace("\t"," ")).strip()
        cleaned_document = cleaned_document.replace("=", " ").replace("-", " ")
        cleaned_document = re.sub(r'\s+', ' ', cleaned_document).strip()
        cleaned_document = (" ").join(cleaned_document.split(" ")) #[:512]
        return cleaned_document

    def clean_query(query: str):
        cleaned_query = query.replace("\n", " ").replace("\r"," ").replace("\t"," ").strip()
        return cleaned_query

    #################################################

    if "wow" in evaluation_datasets[0].lower():
        context_relevance_system_prompt = "You are an expert dialogue agent. "
        context_relevance_system_prompt += "Given the following dialogue and document, you must analyze the provided document and determine whether it is relevant for responding to the dialogue. "
        context_relevance_system_prompt += "In your evaluation, you should consider the content of the document and how it relates to the provided dialogue. "
        context_relevance_system_prompt += 'Output your final verdict by strictly following this format: "[[Yes]]" if the document is relevant and "[[No]]" if the document provided is not relevant. '
        context_relevance_system_prompt += "Do not provide any additional explanation for your decision.\n\n"
    if "fever" in evaluation_datasets[0].lower():
        context_relevance_system_prompt = "You are an expert fact-checking agent. "
        context_relevance_system_prompt += "Given the following statement and document, you must analyze the provided document and determine whether it is sufficient for determining the statement's factuality. "
        context_relevance_system_prompt += "In your evaluation, you should consider the content of the document and how it relates to the provided statement's factuality. "
        context_relevance_system_prompt += 'Output your final verdict by strictly following this format: "[[Yes]]" if the document is sufficient and "[[No]]" if the document is not sufficient. '
        context_relevance_system_prompt += "Do not provide any additional explanation for your decision.\n\n"
    else:
        context_relevance_system_prompt = "Given the following question and document, you must analyze the provided document and determine whether it is sufficient for answering the question. "
        context_relevance_system_prompt += "In your evaluation, you should consider the content of the document and how it relates to the provided question. "
        context_relevance_system_prompt += 'Output your final verdict by strictly following this format: "[[Yes]]" if the document is sufficient and "[[No]]" if the document provided is not sufficient. '
        context_relevance_system_prompt += "Do not provide any additional explanation for your decision.\n\n"

    answer_faithfulness_system_prompt = "Given the following question, document, and answer, you must analyze the provided answer and determine whether it is faithful to the contents of the document. "
    answer_faithfulness_system_prompt += "The answer must not offer new information beyond the context provided in the document. "
    answer_faithfulness_system_prompt += "The answer also must not contradict information provided in the document. "
    answer_faithfulness_system_prompt += 'Output your final verdict by strictly following this format: "[[Yes]]" if the answer is faithful to the document and "[[No]]" if the answer is not faithful to the document. '
    answer_faithfulness_system_prompt += "Do not provide any additional explanation for your decision.\n\n"

    answer_relevance_system_prompt = "Given the following question, document, and answer, you must analyze the provided answer and document before determining whether the answer is relevant for the provided question. "
    answer_relevance_system_prompt += "In your evaluation, you should consider whether the answer addresses all aspects of the question and provides only correct information from the document for answering the question. "
    answer_relevance_system_prompt += 'Output your final verdict by strictly following this format: "[[Yes]]" if the answer is relevant for the given question and "[[No]]" if the answer is not relevant for the given question. '
    answer_relevance_system_prompt += "Do not provide any additional explanation for your decision.\n\n"

    ####################################################################

    for checkpoint, label_column in zip(checkpoints, labels):

        LLM_judge_ratio_predictions = []
        validation_set_lengths = []
        validation_set_ratios = []
        ppi_confidence_intervals = []
        accuracy_scores = []
        for test_set_selection in evaluation_datasets:

            test_set = pd.read_csv(test_set_selection, sep="\t")
            text_column = 'concat_text'
            test_set = test_set[test_set[label_column].notna()]
            if "Context" in label_column:
                test_set[text_column] = [combine_query_document(test_set.iloc[i]['Query'], test_set.iloc[i]['Document']) for i in range(len(test_set))]
            else:
                test_set[text_column] = [combine_query_document(test_set.iloc[i]['Query'], test_set.iloc[i]['Document'], test_set.iloc[i]['Answer']) for i in range(len(test_set))]

            test_set = test_set[test_set[text_column] != "Error"]
            print("Example Text for " + label_column + " Scoring")
            print(test_set.iloc[10][text_column])

            ############################################################

            model_choice = "microsoft/deberta-v3-large"
            max_token_length = 2048
            tokenizer = AutoTokenizer.from_pretrained(model_choice, model_max_length=max_token_length)

            device = "cuda:0"
            device = torch.device(device)
            if not GPT_scoring:
                model = CustomBERTModel(number_of_labels, model_choice)
                model.to(device)

            ############################################################

            eval_dataloader = prepare_dataset_for_evaluation(test_set, label_column, text_column)
            
            if GPT_scoring:
                test_set = test_set.sample(n=2000, random_state=43)
            else:
                print("Loading the Best Finetuned-LLM Checkpoint")
                model.load_state_dict(torch.load(checkpoint))

            ############################################################

            #print("Beginning Evaluation")

            metric = load_metric("accuracy")

            total_predictions = torch.FloatTensor([]).to(device)
            total_references = torch.FloatTensor([]).to(device)
            total_logits = torch.FloatTensor([]).to(device)

            if not GPT_scoring:

                progress_bar = tqdm(range(len(eval_dataloader)))
                model.eval()
                for batch in eval_dataloader:

                    with torch.no_grad():

                        if model_choice in ["mosaicml/mpt-1b-redpajama-200b"]:
                            new_batch = {'ids': batch['input_ids'].to(device), 'mask': batch['attention_mask'].bool().to(device)}
                        else:
                            new_batch = {'ids': batch['input_ids'].to(device), 'mask': batch['attention_mask'].to(device)}

                        if model_choice in ["t5-small", "google/t5-xl-lm-adapt", "google/t5-large-lm-adapt"]:
                            new_batch['decoder_input_ids'] = batch['labels'].reshape(batch['labels'].shape[0], 1).to(device)

                        outputs = model(**new_batch)

                        logits = outputs
                        predictions = torch.argmax(logits, dim=-1)
                        metric.add_batch(predictions=predictions, references=batch['labels'].to(device))

                        total_predictions = torch.cat((total_predictions, predictions), 0)
                        total_references = torch.cat((total_references, batch['labels'].to(device)), 0)
                        total_logits = torch.cat((total_logits, logits), 0)

                        progress_bar.update(1)

            else:

                print("Performing GPT scoring!")
                print("Using gpt model: " + gpt_model)
                if perform_zero_shot:
                    print("Using zero-shot approach")
                    print("Setting few-shot example to None...")
                    few_shot_examples = None

                if "Context_Relevance_Label" == label_column:
                    tqdm.pandas(desc="Generating context relevance scores...", total=test_set.shape[0])
                    test_set["Context_Relevance_Prediction"] = test_set.progress_apply(lambda x: few_shot_context_relevance_scoring(context_relevance_system_prompt, clean_query(x["Query"]), x["Document"], gpt_model, few_shot_examples), axis=1)
                elif "Answer_Faithfulness_Label" == label_column:
                    tqdm.pandas(desc="Generating answer faithfulness scores...", total=test_set.shape[0])
                    test_set["Answer_Faithfulness_Prediction"] = test_set.progress_apply(lambda x: few_shot_answer_faithfulness_scoring(answer_faithfulness_system_prompt, clean_query(x["Query"]), x["Document"], x["Answer"], gpt_model, few_shot_examples), axis=1)
                if "Answer_Relevance_Label" == label_column:
                    tqdm.pandas(desc="Generating answer relevance scores...", total=test_set.shape[0])
                    test_set["Answer_Relevance_Prediction"] = test_set.progress_apply(lambda x: few_shot_answer_relevance_scoring(answer_relevance_system_prompt, clean_query(x["Query"]), x["Document"], x["Answer"], gpt_model, few_shot_examples), axis=1)

                total_predictions = test_set[label_column.replace("_Label", "_Prediction")]
                total_references = test_set[label_column]

                
            ############################################################

            results = metric.compute(references=total_references, predictions=total_predictions)
            #print("Validation Accuracy for " + test_set_selection + " - " + label_column + ": " + str(results['accuracy']))
            #print("-------------------------------------------------------")
            #print("Positive Reference Label Count: " + str(total_references.tolist().count(1)))
            #print("Negative Reference Label Count: " + str(total_references.tolist().count(0)))
            #print("-------------------------------------------------------")
            #print("Positive Prediction Label Count: " + str(total_predictions.tolist().count(1)))
            #print("Negative Prediction Label Count: " + str(total_predictions.tolist().count(0)))
            #print("-------------------------------------------------------")

            ########################################################################

            prediction_column = label_column + "_Model_Predictions"
            test_set[prediction_column] = total_predictions.tolist()
            test_set = test_set[test_set[label_column].notna()]
            for label in labels:
                if label != label_column:
                    test_set = test_set[test_set[label] != 0]

            if use_pseudo_human_labels:
                y_labeled_ratio = Y_labeled_count / len(test_set)
                Yhat_unlabeled_dataset, Y_labeled_dataset = train_test_split(test_set, test_size=y_labeled_ratio, random_state=42)
                Yhat_unlabeled_dataset = test_set
            else:
                Y_labeled_dataset = pd.read_csv(gold_label_path, sep="\t")
                Yhat_unlabeled_dataset = test_set

            Y_labeled = Y_labeled_dataset[label_column].values.astype(int)
            Yhat_labeled = Y_labeled_dataset[prediction_column].values.astype(int)
            Yhat_unlabeled = Yhat_unlabeled_dataset[prediction_column].values.astype(int)
            
            #print("Y_labeled, Yhat_labeled, Yhat_unlabeled for " + test_set_selection + " - " + label_column)
            #print(len(Y_labeled))
            #print(len(Yhat_labeled))
            #print(len(Yhat_unlabeled))
            #print("Positive/Negative Ratio for " + test_set_selection + " - " + label_column)
            #print(Yhat_unlabeled_dataset[label_column].tolist().count(1))
            #print(Yhat_unlabeled_dataset[label_column].tolist().count(0))
            #print(Yhat_unlabeled_dataset[label_column].tolist().count(1) / len(Yhat_unlabeled_dataset))
            #print(Yhat_unlabeled_dataset[label_column].tolist().count(0) / len(Yhat_unlabeled_dataset))

            ######################################################################

            avg_ci, avg_ci_classical, ci_imputed = calculate_ppi(Y_labeled,  Yhat_labeled, Yhat_unlabeled, alpha, num_trials)
            LLM_judge_prediction = sum(avg_ci) / len(avg_ci)
            #print("PPI Confidence Interval for " + test_set_selection + " - " + label_column)
            #print(avg_ci)
            #print(LLM_judge_prediction)
            #print("--------------------------------------------------\n")

            LLM_judge_ratio_predictions.append(LLM_judge_prediction)
            validation_set_lengths.append(len(test_set))
            validation_set_ratios.append(round(Yhat_unlabeled_dataset[label_column].tolist().count(1) / len(Yhat_unlabeled_dataset), 3))
            ppi_confidence_intervals.append(avg_ci.tolist())
            accuracy_scores.append(results['accuracy'])

        ######################################################################

        indexed_list = list(enumerate(LLM_judge_ratio_predictions))
        sorted_list = sorted(indexed_list, key=lambda x: x[1])
        sorted_indices = [index for index, _ in sorted_list]
        tau, p_value = stats.kendalltau(correct_ranking, sorted_indices)

        print("--------------------------------------------------")
        print(label_column + " Scoring")
        print("Correct Ranking v. LLM-as-a-Judge Ranking")
        print(correct_ranking)
        print(sorted_indices)
        print("Kendall's Tau: " + str(tau))
        print("P-Value: " + str(p_value))
        print("Avg. PPIs: " + str(LLM_judge_ratio_predictions))
        print("PPI Confidence Intervals: " + str(ppi_confidence_intervals))
        print("Validation Set Lengths: " + str(validation_set_lengths))
        print("Validation Set Ratio: " + str(validation_set_ratios))
        print("Test Accuracy Scores: " + str(accuracy_scores))
        print("Y-Labeled Example Count: " + str(len(Y_labeled)))
        print("--------------------------------------------------\n")
