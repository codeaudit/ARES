## ARES: An Automated Evaluation Framework for Retrieval-Augmented Generation Systems

Paper: [ARES: An Automated Evaluation Framework for Retrieval-Augmented Generation Systems](http://tiny.cc/ares_rag)

To implement ARES for scoring your RAG system and comparing to other RAG configurations, you need three components:
​
- A **human preference validation set** of annotated query, document, and answer triples for the evaluation criteria (e.g. context relevance, answer faithfulness, and/or answer relevance). There should be at least 50 examples but several hundred examples is ideal.
- A **set of few-shot examples** for scoring context relevance, answer faithfulness, and/or answer relevance in your system
- A much larger **set of unlabeled query-document-answer triples** outputted by your RAG system for scoring


The ARES training pipeline is three steps:
​
1) Generate synthetic queries and answers from in-domain passages
2) Prepare LLM judges for scoring RAG system by fine-tuning on synthetically-generated training data
3) Deploy the prepared LLM judges to evaluate your RAG system across key performance metrics


Note: We also allow users to skip Steps #1 and #2 deploying a zero/few-shot LLM-as-a-Judge
​
### Installation
​
To install the necessary dependencies, run the following commands:
​
````
conda create -n llm_judge python=3.10
conda activate llm_judge
pip install -r requirements.txt
````
​
Additionally, you will need to initialize an OpenAI API key with the following command:
````
export OPENAI_API_KEY=<your key here>
````
​
## Step #1: Synthetic Data Generation
​
To generate synthetic training data, use `LLM-as-a-Judge_Adaptation/Generate_Synthetic_Queries_and_Answers.py`. Replace items in the following command with your dataset and configuration:
​
````
python Generate_Synthetic_Queries_and_Answers.py \
       --document_filepath <document_filepath> \
       --few_shot_prompt_filename <few_shot_prompt_filename> \
       --synthetic_queries_filename <synthetic_queries_filename> \
       --documents_sampled 10000
````

This script will output a filepath to the generated synthetic queries for the next step.
​

Note: For examples files for `document_filepath` and `few_shot_prompt_filename`, please see `example_files`.
​
## Step #2: Fine-tune LLM-as-a-Judge
​
With the generated file under `synthetic_queries_filename` from the previous step, use `LLM-as-a-Judge_Adaptation/General_Binary_Classifier.py` to train your LLM-as-a-Judge with the following command:
​
````
python General_Binary_Classifier.py \
       --classification_dataset <synthetic queries file> \
       --test_set_selection <test_set_selection> \
       --label_column Context_Relevance_Label \
       --num_epochs 10 \
       --patience_value 3 \
       --learning_rate 5e-6
````

For `document_filepath`, put the filepath of the synthetic queries generated in the previous step. For `test_set_selection`, put the filepath of the human annotated examples of your dataset; it should be formatted like the file `example_files/evaluation_datasets.tsv`.

This script will output a model checkpoint path for the next step.


## Step #3: Score RAG System with ARES
​
With the outputted model checkpoint from Step #2, you can now score your RAG system's configurations using ARES with following command in folder `RAG_Automatic_Evaluation/`:
​
````
python LLMJudge_RAG_Compared_Scoring.py \
       --alpha 0.05 \
       --num_trials 1000 \
       --evaluation_datasets <evaluation_datasets as list> \
       --checkpoints <checkpoints as list> \
       --labels <label columns as list> \
       --GPT_scoring <True or False> \
       --gold_label_path <gold_label_path>
       --swap_human_labels_for_gpt_labels False
````
​
For `evaluation_datasets`, we expect a list of filepaths to query-passage-answer TSVs for each RAG configuration you wish to score.

If you want to use few-shot GPT scoring, switch `GPT_scoring` to `True`. You can leave the `checkpoints` list as blank and specify the GPT model with the tag `--gpt_model <model selected>`.
​

Note: For examples files of `evaluation_datasets` and `gold_label_path`, please see `example_files/evaluation_datasets.tsv` for formatting.

## Results Replication

We include synthetic datasets for key experimental results in `synthetic_datasets`. The few-shot prompts used for generation and evaluation are included in `datasets_v2`. We also include instructions for fine-tuning LLM judges in the paper itself. Please reach out to jonsaadfalcon@stanford.edu if you have any further questions.

## Citation

To cite our work, please use the following Bibtex:
