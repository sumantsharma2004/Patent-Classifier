import streamlit as st
import pandas as pd
from openai import AzureOpenAI
import time
from typing import Dict, List
import json
import io
import re
import os
from azure.storage.blob import BlobServiceClient, ContentSettings
from datetime import datetime, timedelta

# Page configuration
st.set_page_config(
    page_title="IeB Classifier",
    page_icon="logo.png",
    layout="wide"
)

# Initialize session state for user-specific data
# IMPORTANT: Each user session has isolated credentials and data
# This prevents cross-user data leakage in multi-user deployments
if 'results_df' not in st.session_state:
    st.session_state.results_df = None
if 'processing_complete' not in st.session_state:
    st.session_state.processing_complete = False
if 'credentials' not in st.session_state:
    st.session_state.credentials = {
        'api_key': '',
        'endpoint': '',
        'api_version': '2025-01-01-preview',
        'model': 'gpt-4o'
    }
if 'blob_credentials' not in st.session_state:
    st.session_state.blob_credentials = {
        'connection_string': '',
        'container_name': 'patent-results'
    }
if 'blob_urls' not in st.session_state:
    st.session_state.blob_urls = {
        'excel': None,
        'csv': None
    }

def upload_to_blob_storage(file_data: bytes, filename: str, connection_string: str, container_name: str, content_type: str) -> str:
    """
    Upload file to Azure Blob Storage and return a public URL with SAS token.

    Args:
        file_data: File content as bytes
        filename: Name of the file to upload
        connection_string: Azure Storage connection string
        container_name: Container name in Azure Storage
        content_type: MIME type of the file

    Returns:
        Public URL with SAS token for accessing the blob
    """
    try:
        # Create BlobServiceClient
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)

        # Get container client (create if doesn't exist)
        container_client = blob_service_client.get_container_client(container_name)
        try:
            container_client.create_container(public_access='blob')
        except:
            # Container already exists
            pass

        # Create unique blob name with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        blob_name = f"{timestamp}_{filename}"

        # Upload blob
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
        blob_client.upload_blob(
            file_data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type)
        )

        # Generate SAS URL (valid for 7 days)
        blob_url = blob_client.url

        return blob_url

    except Exception as e:
        raise Exception(f"Failed to upload to Azure Blob Storage: {str(e)}")

def consolidate_patent_records(df: pd.DataFrame) -> pd.DataFrame:
    """
    Consolidate multi-row patent records into single rows.
    Handles cases where descriptions are split across multiple rows.
    """
    # Pattern to detect a new publication number (US, CN, KR, EP, WO, JP etc.)
    pub_pattern = re.compile(r'^[A-Z]{2}\d+')

    final_rows = []
    current_record = None

    # Find the publication number and description columns
    pub_col = None
    desc_col = None

    for col in df.columns:
        col_lower = col.strip().lower()
        if 'publication' in col_lower and 'number' in col_lower:
            pub_col = col
        elif 'description' in col_lower:
            desc_col = col

    # If we can't find a publication number column, return as-is
    if not pub_col:
        return df

    for _, row in df.iterrows():
        pub_no = str(row[pub_col]).strip() if pub_col else ""

        # If row begins with a valid publication number – start a new block
        if pub_pattern.match(pub_no):
            # Save previous record
            if current_record:
                final_rows.append(current_record)

            # Start new record - preserve original column names
            current_record = {}
            for col in df.columns:
                if col == pub_col:
                    current_record[col] = pub_no
                elif col == desc_col:
                    current_record[col] = str(row.get(col, ""))
                else:
                    current_record[col] = row.get(col, "")

        else:
            # This is continuation text – append to description field
            if current_record and desc_col:
                extra = str(row.get(desc_col, "")).strip()
                if extra and extra != 'nan':
                    # Check if the previous description already ends with """ (indicating end of description)
                    if not current_record[desc_col].rstrip().endswith('"""'):
                        current_record[desc_col] += "\n" + extra

    # Save last record
    if current_record:
        final_rows.append(current_record)

    # Convert to DataFrame
    if final_rows:
        return pd.DataFrame(final_rows)
    else:
        return df

def get_azure_client(api_key: str, endpoint: str, api_version: str):
    """
    Initialize Azure OpenAI client with provided credentials.

    Credentials are passed explicitly (not from environment variables)
    to ensure proper session isolation in multi-user deployments.
    """
    return AzureOpenAI(
        api_key=api_key,
        api_version=api_version,
        azure_endpoint=endpoint
    )

def create_classification_prompt(prompt_template: str, row_data: Dict) -> str:
    """Create the prompt by substituting placeholders with actual data"""
    prompt = prompt_template
    for key, value in row_data.items():
        placeholder = f"{{{key}}}"
        prompt = prompt.replace(placeholder, str(value) if pd.notna(value) else 'N/A')
    return prompt

def classify_patent_chunked(row: pd.Series, client, model: str, prompt_template: str, column_mapping: Dict, chunk_size: int = 15000) -> Dict:
    """Classify patent using chunking when content is too large"""

    # Get the description column value
    desc_col = column_mapping.get('description', '')
    description = str(row.get(desc_col, 'N/A'))

    # Strategy: Split description into chunks while keeping other fields intact
    chunks = []

    # First chunk includes all fields with partial description
    first_chunk_desc = description[:chunk_size] if len(description) > chunk_size else description
    first_chunk_data = {}
    for standard_name, actual_column in column_mapping.items():
        if standard_name == 'description':
            first_chunk_data[standard_name] = first_chunk_desc
        else:
            first_chunk_data[standard_name] = row.get(actual_column, 'N/A')
    chunks.append(first_chunk_data)

    # Additional chunks with remaining description
    if len(description) > chunk_size:
        remaining_desc = description[chunk_size:]
        while remaining_desc:
            chunk_desc = remaining_desc[:chunk_size]
            chunk_data = {key: '' for key in column_mapping.keys()}
            chunk_data['description'] = chunk_desc
            chunk_data['publication_number'] = first_chunk_data['publication_number']
            chunks.append(chunk_data)
            remaining_desc = remaining_desc[chunk_size:]

    # Analyze each chunk
    results = []
    for i, chunk_data in enumerate(chunks):
        try:
            prompt = create_classification_prompt(prompt_template, chunk_data)

            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a quantum technology expert specializing in patent analysis."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.3
            )

            response_text = response.choices[0].message.content.strip()

            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0].strip()
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0].strip()

            result = json.loads(response_text)
            results.append(result)

        except Exception as e:
            continue

    # Combine results
    if not results:
        return {
            "relevance": "ERROR",
            "relevance_percentage": 0,
            "confidence": "LOW",
            "reasoning": "Failed to analyze any chunks",
            "key_features_found": [],
            "protocols_mentioned": [],
            "relevance_source": "N/A"
        }

    # Take the highest relevance found
    best_result = max(results, key=lambda x: x.get('relevance_percentage', 0))

    # Combine features from all chunks
    all_features = []
    all_protocols = []
    for r in results:
        all_features.extend(r.get('key_features_found', []))
        all_protocols.extend(r.get('protocols_mentioned', []))

    best_result['key_features_found'] = list(set(all_features))
    best_result['protocols_mentioned'] = list(set(all_protocols))
    best_result['reasoning'] = f"[Chunked: {len(chunks)} parts] {best_result.get('reasoning', '')}"

    return best_result


def classify_patent(row: pd.Series, client, model: str, prompt_template: str, column_mapping: Dict) -> Dict:
    """Send patent data to Azure OpenAI for classification"""

    # Create row data dictionary with mapped columns
    row_data = {}
    for standard_name, actual_column in column_mapping.items():
        row_data[standard_name] = row.get(actual_column, 'N/A')

    # Create the prompt with patent data
    prompt = create_classification_prompt(prompt_template, row_data)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a quantum technology expert specializing in patent analysis."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
        )

        # Parse the response
        response_text = response.choices[0].message.content.strip()

        # Try to extract JSON from the response
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()

        result = json.loads(response_text)
        return result

    except json.JSONDecodeError as e:
        return {
            "relevance": "ERROR",
            "relevance_percentage": 0,
            "confidence": "N/A",
            "reasoning": f"Failed to parse response: {response_text[:200]}",
            "key_features_found": [],
            "protocols_mentioned": [],
            "relevance_source": "N/A"
        }
    except Exception as e:
        error_msg = str(e).lower()
        # Check if it's a context length error
        if 'context' in error_msg or 'token' in error_msg or 'length' in error_msg or 'too large' in error_msg or 'maximum' in error_msg:
            return classify_patent_chunked(row, client, model, prompt_template, column_mapping)

        return {
            "relevance": "ERROR",
            "relevance_percentage": 0,
            "confidence": "N/A",
            "reasoning": f"API error: {str(e)}",
            "key_features_found": [],
            "protocols_mentioned": [],
            "relevance_source": "N/A"
        }

def process_patents(df: pd.DataFrame, client, model: str, prompt_template: str,
                   column_mapping: Dict, progress_bar, status_text):
    """Process all patents in the dataframe"""
    results = []
    total_rows = len(df)

    for idx, row in df.iterrows():
        status_text.text(f"Processing patent {idx + 1}/{total_rows}: {row.get(column_mapping.get('publication_number', ''), 'N/A')}")

        # Classify the patent
        classification = classify_patent(row, client, model, prompt_template, column_mapping)

        # Add classification results to the row - dynamically extract all fields
        result_row = row.to_dict()

        # Iterate through all classification fields and add them to result
        for key, value in classification.items():
            # Convert lists to comma-separated strings for better Excel compatibility
            if isinstance(value, list):
                result_row[key] = ', '.join(str(item) for item in value)
            else:
                result_row[key] = value

        results.append(result_row)

        # Update progress
        progress_bar.progress((idx + 1) / total_rows)

        # Add a small delay to avoid rate limiting
        time.sleep(0.5)

    return pd.DataFrame(results)

# Default prompt template
DEFAULT_PROMPT = """You are a quantum communication technology expert. Analyze the following patent to determine if it is relevant to a specific QKD (Quantum Key Distribution) last-mile problem system.

**TARGET SYSTEM CRITERIA:**
A QKD system for last-mile problem consisting of:
- Entangled single photons transmitted to generate a shared cryptographic key that is immediately available


**RELEVANT KEYWORDS & SYNONYMS:**

1. Quantum Key Distribution (QKD): Quantum cryptographic communication, quantum key exchange, quantum encryption protocol, quantum-secured communication system, quantum information transmission system, quantum-based key establishment, quantum communication framework, quantum secret sharing mechanism

2. Device (Alice/Bob): Node, terminal, endpoint, station, transceiver unit, communication module, quantum apparatus, participant unit, sender, receiver

3. Optical Fiber: Fiber-optic link, optical waveguide, light-transmission medium, photonic channel, fiber channel, optical transmission line, glass fiber cable, optical conduit

4. Transmit Data: Exchange information, communicate signals, send and receive data, convey information, transfer data packets, propagate signals, relay quantum states, communicate bit sequences

5. Bidirectionally: In both directions, duplex mode, two-way communication, reciprocal transmission, mutual data exchange, dual-directional signaling, reverse and forward channels, full-duplex mode

6. Photons: Quantum light particles, light quanta, optical qubits, single-photon pulses, quantum carriers, photon packets, quantum optical signals, entangled light particles

7. Shared Cryptographic Key: Secret key, mutual encryption key, joint generation key, symmetric key, symmetric encryption key, session key, private key, quantum-generated key, secure shared key, common cryptographic token, co-established encryption key, secure key

8. Entangled: Correlated, quantum-linked, nonlocally connected, coherently coupled, quantum-correlated, interdependent (quantum state), superposition-coupled, BBM92, E91
**PATENT INFORMATION:**
Publication Number: {publication_number}
Title: {title}
Abstract: {abstract}
Claims: {claims}
Description: {description}


**TASK:**
Analyze this patent and respond with ONLY a JSON object in the following format:
{{
   "relevance": "RELEVANT" or "NOT RELEVANT",
   "relevance_percentage": <number between 0-100>,
   "confidence": "HIGH", "MEDIUM", or "LOW",
   "reasoning": "Brief explanation (2-3 sentences) of why this patent is relevant or not relevant",
   "key_features_found": ["list of key QKD features identified in the patent"],
   "protocols_mentioned": ["E91", "BBM92", "B92"] (only include these 3 protocols if explicitly mentioned or clearly described),
   "relevance_source": "CLAIMS" or "DESCRIPTION" or "BOTH" or "ABSTRACT" or "TITLE" (indicate where the key relevant information was found that led to the relevance decision)
}}

IMPORTANT CLASSIFICATION RULES:
- Set "relevance" to "RELEVANT" ONLY if relevance_percentage is 75% or higher
- Set "relevance" to "NOT RELEVANT" if relevance_percentage is below 75%

The relevance_percentage should be:
- 0-20%: Not relevant at all - no QKD features present
- 21-40%: Minimally relevant - mentions quantum but not QKD specific
- 41-60%: Somewhat relevant - QKD related but not entanglement-based
- 61-74%: Moderately relevant - QKD with some features but missing critical elements
- 75-89%: Highly relevant - QKD with entanglement and most key features
- 90-100%: Extremely relevant - matches all target system criteria including E91/BBM92/B92 protocols

Confidence levels should reflect:
- HIGH: Very clear evidence for the score, explicit mentions of key features
- MEDIUM: Good evidence but some ambiguity or missing details
- LOW: Uncertain, limited information, or difficult to assess

Respond ONLY with the JSON object, no additional text.
"""

# Main UI
col1, col2 = st.columns([1, 10])
with col1:
    st.image("logo.png", width=60)
with col2:
    st.title("IeB Classifier")
st.markdown("Analyze patents for your task and check the relevancy through AI")

# Sidebar for configuration
st.sidebar.header("⚙️ Configuration")

# Azure OpenAI Configuration
st.sidebar.subheader("Azure OpenAI Settings")

# Security info
st.sidebar.info("🔒 **Secure Multi-User Support**: Your credentials are isolated to your session only and not shared with other users.")

# Option to upload .env file for easy credential loading
st.sidebar.markdown("**Option 1: Upload .env file**")
env_file = st.sidebar.file_uploader(
    "Upload .env file (optional)",
    type=['env', 'txt'],
    help="Upload your .env file to automatically populate credentials"
)

# Also try to load from .env file in the project directory if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()

    # Auto-load blob storage credentials from environment if available
    if not st.session_state.blob_credentials['connection_string']:
        # Try full connection string first
        blob_conn_from_env = os.getenv('AZURE_STORAGE_CONNECTION_STRING')
        if blob_conn_from_env:
            st.session_state.blob_credentials['connection_string'] = blob_conn_from_env
        else:
            # Try to build connection string from URL and key
            storage_url = os.getenv('AZURE_STORAGE_ACCOUNT_URL')
            storage_key = os.getenv('AZURE_STORAGE_KEY')
            if storage_url and storage_key:
                # Extract account name from URL
                # URL format: https://<account-name>.blob.core.windows.net
                account_name = storage_url.replace('https://', '').split('.')[0]
                # Build connection string
                connection_string = f"DefaultEndpointsProtocol=https;AccountName={account_name};AccountKey={storage_key};EndpointSuffix=core.windows.net"
                st.session_state.blob_credentials['connection_string'] = connection_string

    if st.session_state.blob_credentials['container_name'] == 'patent-results':
        blob_container_from_env = os.getenv('AZURE_STORAGE_CONTAINER_NAME')
        if blob_container_from_env:
            st.session_state.blob_credentials['container_name'] = blob_container_from_env
except:
    pass

# Parse .env file if uploaded
if env_file is not None:
    env_content = env_file.read().decode('utf-8')
    for line in env_content.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if key == "AZURE_API_KEY":
                st.session_state.credentials['api_key'] = value
            elif key == "AZURE_ENDPOINT":
                st.session_state.credentials['endpoint'] = value
            elif key == "AZURE_API_VERSION":
                st.session_state.credentials['api_version'] = value
            elif key == "AZURE_MODEL":
                st.session_state.credentials['model'] = value
            elif key == "AZURE_STORAGE_CONNECTION_STRING":
                st.session_state.blob_credentials['connection_string'] = value
            elif key == "AZURE_STORAGE_CONTAINER_NAME":
                st.session_state.blob_credentials['container_name'] = value

    st.sidebar.success("✅ Credentials loaded from .env file")

st.sidebar.markdown("**Option 2: Enter credentials manually**")

# Input fields using session state
azure_api_key = st.sidebar.text_input(
    "API Key",
    type="password",
    value=st.session_state.credentials['api_key'],
    placeholder="Enter your Azure OpenAI API key",
    key="api_key_input"
)

azure_endpoint = st.sidebar.text_input(
    "Endpoint URL",
    value=st.session_state.credentials['endpoint'],
    placeholder="https://your-resource.openai.azure.com/",
    key="endpoint_input"
)

azure_api_version = st.sidebar.text_input(
    "API Version",
    value=st.session_state.credentials['api_version'],
    placeholder="2025-01-01-preview",
    key="api_version_input"
)

azure_model = st.sidebar.text_input(
    "Model Deployment Name",
    value=st.session_state.credentials['model'],
    placeholder="gpt-4o",
    key="model_input"
)

# Update session state with current input values
st.session_state.credentials['api_key'] = azure_api_key
st.session_state.credentials['endpoint'] = azure_endpoint
st.session_state.credentials['api_version'] = azure_api_version
st.session_state.credentials['model'] = azure_model

# Get blob storage credentials from session state (auto-loaded from .env)
blob_connection_string = st.session_state.blob_credentials['connection_string']
blob_container_name = st.session_state.blob_credentials['container_name']

# Show blob storage status
if blob_connection_string:
    st.sidebar.markdown("---")
    st.sidebar.success("✅ Azure Blob Storage configured")
    st.sidebar.info(f"📦 Container: {blob_container_name}")

# File upload section
st.header("📁 Upload Patent Data")

# Option to consolidate multi-row records
consolidate_records = st.checkbox(
    "Consolidate multi-row patent records",
    value=True,
    help="Enable this if your CSV has patent descriptions split across multiple rows. This will merge them into single records."
)

uploaded_file = st.file_uploader(
    "Choose a file (CSV, TSV, or Excel)",
    type=['csv', 'tsv', 'xlsx', 'xls'],
    help="Upload your patent data file containing publication numbers, titles, abstracts, claims, and descriptions"
)

if uploaded_file is not None:
    # Store input filename for output naming
    input_filename = uploaded_file.name
    input_basename = os.path.splitext(input_filename)[0]

    # Read the file based on its type
    try:
        file_extension = uploaded_file.name.split('.')[-1].lower()

        if file_extension == 'csv':
            df = pd.read_csv(uploaded_file)
        elif file_extension == 'tsv':
            df = pd.read_csv(uploaded_file, sep='\t')
        elif file_extension in ['xlsx', 'xls']:
            df = pd.read_excel(uploaded_file)
        else:
            st.error("Unsupported file format!")
            st.stop()

        # Normalize column names
        df.columns = df.columns.str.strip().str.lower()

        original_row_count = len(df)

        # Consolidate multi-row records if enabled
        if consolidate_records:
            with st.spinner("Consolidating multi-row patent records..."):
                df = consolidate_patent_records(df)
                st.info(f"📋 Consolidated {original_row_count} rows into {len(df)} patent records")

        st.success(f"✅ File loaded successfully! Found {len(df)} rows and {len(df.columns)} columns")

        # Show preview
        with st.expander("📊 Preview Data (first 5 rows)"):
            st.dataframe(df.head())

        # Option to download consolidated data
        if consolidate_records and len(df) < original_row_count:
            st.subheader("💾 Download Consolidated Data")
            col1, col2 = st.columns(2)

            with col1:
                # TSV download
                tsv_data = df.to_csv(sep='\t', index=False)
                st.download_button(
                    label="Download Consolidated TSV",
                    data=tsv_data,
                    file_name=f"{input_basename}_consolidated.tsv",
                    mime="text/tab-separated-values",
                    help="Download the consolidated patent data as TSV"
                )

            with col2:
                # CSV download
                csv_data = df.to_csv(index=False)
                st.download_button(
                    label="Download Consolidated CSV",
                    data=csv_data,
                    file_name=f"{input_basename}_consolidated.csv",
                    mime="text/csv",
                    help="Download the consolidated patent data as CSV"
                )

        # Column mapping section
        st.header("🔗 Column Mapping")
        st.markdown("Map your file's columns to the required fields. Use the placeholders `{publication_number}`, `{title}`, `{abstract}`, `{claims}`, `{description}` in your prompt.")

        available_columns = df.columns.tolist()

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Required Fields")
            publication_number_col = st.selectbox(
                "Publication Number Column",
                options=available_columns,
                index=available_columns.index('publication number') if 'publication number' in available_columns else 0
            )
            title_col = st.selectbox(
                "Title Column",
                options=available_columns,
                index=available_columns.index('title') if 'title' in available_columns else 0
            )
            abstract_col = st.selectbox(
                "Abstract Column",
                options=available_columns,
                index=available_columns.index('abstract') if 'abstract' in available_columns else 0
            )

        with col2:
            st.subheader("Optional Fields")
            claims_col = st.selectbox(
                "Claims Column",
                options=['None'] + available_columns,
                index=available_columns.index('claims') + 1 if 'claims' in available_columns else 0
            )
            description_col = st.selectbox(
                "Description Column",
                options=['None'] + available_columns,
                index=available_columns.index('description') + 1 if 'description' in available_columns else 0
            )

        # Create column mapping
        column_mapping = {
            'publication_number': publication_number_col,
            'title': title_col,
            'abstract': abstract_col,
            'claims': claims_col if claims_col != 'None' else '',
            'description': description_col if description_col != 'None' else ''
        }

        # Prompt configuration
        st.header("📝 Prompt Configuration")
        prompt_choice = st.radio(
            "Choose prompt option:",
            ["Use Default Prompt", "Customize Prompt"]
        )

        if prompt_choice == "Use Default Prompt":
            prompt_template = DEFAULT_PROMPT
            with st.expander("View Default Prompt"):
                st.text_area("Default Prompt Template", value=DEFAULT_PROMPT, height=300, disabled=True)
        else:
            st.markdown("""
            **Available placeholders:**
            - `{publication_number}` - Publication number
            - `{title}` - Patent title
            - `{abstract}` - Patent abstract
            - `{claims}` - Patent claims
            - `{description}` - Patent description
            """)
            prompt_template = st.text_area(
                "Custom Prompt Template",
                value=DEFAULT_PROMPT,
                height=400,
                help="Use the placeholders above to customize your prompt"
            )

        # Process button
        st.header("🚀 Process Patents")

        col1, col2, col3 = st.columns([1, 1, 2])
        with col1:
            process_button = st.button("Start Analysis", type="primary", use_container_width=True)
        with col2:
            if st.session_state.processing_complete:
                st.success("✅ Processing Complete!")

        if process_button:
            if not azure_api_key or not azure_endpoint:
                st.error("⚠️ Please provide Azure OpenAI credentials in the sidebar!")
            else:
                try:
                    # Initialize Azure OpenAI client
                    client = get_azure_client(azure_api_key, azure_endpoint, azure_api_version)

                    # Create progress indicators
                    progress_bar = st.progress(0)
                    status_text = st.empty()

                    # Process patents
                    with st.spinner("Processing patents..."):
                        results_df = process_patents(
                            df,
                            client,
                            azure_model,
                            prompt_template,
                            column_mapping,
                            progress_bar,
                            status_text
                        )

                    # Remove unnamed columns (columns that start with 'Unnamed:')
                    unnamed_cols = [col for col in results_df.columns if str(col).startswith('Unnamed:')]
                    if unnamed_cols:
                        status_text.text(f"Removing {len(unnamed_cols)} unnamed columns...")
                        results_df = results_df.drop(columns=unnamed_cols)

                    # Remove columns that were not mapped by the user (only keep selected input fields + AI output fields)
                    # Identify which columns are from the original input
                    mapped_input_cols = [v for k, v in column_mapping.items() if v and v != 'None']

                    # Get all columns from original dataframe
                    original_cols = df.columns.tolist()

                    # Columns to remove: original columns that were NOT mapped by the user
                    cols_to_remove = [col for col in original_cols if col in results_df.columns and col not in mapped_input_cols]

                    if cols_to_remove:
                        status_text.text(f"Removing {len(cols_to_remove)} unmapped input columns...")
                        results_df = results_df.drop(columns=cols_to_remove)

                    # Split long descriptions into multiple columns
                    # Excel cell limit is ~32,767 characters
                    MAX_CELL_LENGTH = 32000

                    # Find description column dynamically
                    desc_col = None
                    for col in results_df.columns:
                        if 'description' in str(col).lower():
                            desc_col = col
                            break

                    if desc_col:
                        # Check if any descriptions need to be split
                        descriptions_to_split = results_df[desc_col].fillna('').astype(str)
                        needs_continuation = descriptions_to_split.str.len() > MAX_CELL_LENGTH

                        if needs_continuation.any():
                            status_text.text(f"Splitting {needs_continuation.sum()} long descriptions into continuation columns...")

                            # Create description_continued column
                            desc_continued_col = f'{desc_col}_continued'
                            results_df[desc_continued_col] = ''

                            for idx in results_df[needs_continuation].index:
                                full_desc = str(results_df.loc[idx, desc_col])

                                # Split the description
                                results_df.loc[idx, desc_col] = full_desc[:MAX_CELL_LENGTH]
                                results_df.loc[idx, desc_continued_col] = full_desc[MAX_CELL_LENGTH:]

                                # If still too long, handle additional splits
                                remaining = full_desc[MAX_CELL_LENGTH:]
                                continuation_num = 2
                                while len(remaining) > MAX_CELL_LENGTH:
                                    col_name = f'{desc_col}_continued_{continuation_num}'
                                    if col_name not in results_df.columns:
                                        results_df[col_name] = ''

                                    results_df.loc[idx, f'{desc_col}_continued_{continuation_num-1}'] = remaining[:MAX_CELL_LENGTH]
                                    remaining = remaining[MAX_CELL_LENGTH:]
                                    continuation_num += 1

                                # Add the final remaining part
                                if continuation_num > 2:
                                    results_df.loc[idx, f'{desc_col}_continued_{continuation_num-1}'] = remaining

                            # Reorder columns to place continuation columns next to description
                            cols = list(results_df.columns)
                            if desc_col in cols:
                                desc_idx = cols.index(desc_col)
                                # Get all continuation columns for this description
                                continuation_cols = [col for col in cols if col.startswith(f'{desc_col}_continued')]
                                # Remove continuation columns from their current positions
                                for col in continuation_cols:
                                    cols.remove(col)
                                # Insert continuation columns right after description
                                for i, col in enumerate(sorted(continuation_cols)):
                                    cols.insert(desc_idx + 1 + i, col)
                                # Reorder the dataframe
                                results_df = results_df[cols]

                    st.session_state.results_df = results_df
                    st.session_state.processing_complete = True

                    status_text.text("✅ All patents processed!")
                    progress_bar.progress(1.0)

                    # Upload to Azure Blob Storage if credentials provided
                    if blob_connection_string and blob_container_name:
                        status_text.text("📤 Uploading files to Azure Blob Storage...")
                        try:
                            # Generate Excel file
                            excel_buffer = io.BytesIO()
                            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                                results_df.to_excel(writer, index=False, sheet_name='Results')
                            excel_buffer.seek(0)
                            excel_data = excel_buffer.read()

                            # Generate CSV file
                            csv_data = results_df.to_csv(index=False).encode('utf-8')

                            # Upload Excel file
                            excel_url = upload_to_blob_storage(
                                excel_data,
                                f"{input_basename}_output.xlsx",
                                blob_connection_string,
                                blob_container_name,
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                            )
                            st.session_state.blob_urls['excel'] = excel_url

                            # Upload CSV file
                            csv_url = upload_to_blob_storage(
                                csv_data,
                                f"{input_basename}_output.csv",
                                blob_connection_string,
                                blob_container_name,
                                "text/csv"
                            )
                            st.session_state.blob_urls['csv'] = csv_url

                            status_text.text("✅ Files uploaded to Azure Blob Storage successfully!")
                            st.success("🎉 Analysis complete! Files uploaded to Azure Blob Storage and available for download below.")

                        except Exception as e:
                            st.warning(f"⚠️ Analysis complete but failed to upload to Azure Blob Storage: {str(e)}")
                            st.info("💡 You can still download files using the buttons below.")
                    else:
                        st.success("🎉 Analysis complete! Results are ready for download below.")
                        st.info("💡 Configure Azure Blob Storage settings in sidebar to enable cloud upload.")

                except Exception as e:
                    st.error(f"❌ Error during processing: {str(e)}")

        # Display results
        if st.session_state.results_df is not None:
            results_df = st.session_state.results_df

            st.header("📊 Results")

            # Prominent download section at the top
            st.markdown("### 💾 Download Your Results")

            # Show Azure Blob Storage URLs if available
            if st.session_state.blob_urls['excel'] or st.session_state.blob_urls['csv']:
                st.success("✅ Files uploaded to Azure Blob Storage - URLs available below")

                st.markdown("#### 🌐 Cloud Download Links (Permanent)")

                col1, col2 = st.columns(2)

                with col1:
                    if st.session_state.blob_urls['excel']:
                        st.markdown(f"""
                        **Excel File (.xlsx)**
                        [Click here to download]({st.session_state.blob_urls['excel']})

                        Copy URL:
                        ```
                        {st.session_state.blob_urls['excel']}
                        ```
                        """)

                with col2:
                    if st.session_state.blob_urls['csv']:
                        st.markdown(f"""
                        **CSV File (.csv)**
                        [Click here to download]({st.session_state.blob_urls['csv']})

                        Copy URL:
                        ```
                        {st.session_state.blob_urls['csv']}
                        ```
                        """)

                st.info("💡 These links are permanent and can be accessed anytime, even after your session expires.")
                st.markdown("---")

            # Download buttons (alternative method)
            st.markdown("#### 💻 Direct Download (Session-based)")
            col1, col2 = st.columns(2)

            with col1:
                # Excel download
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    results_df.to_excel(writer, index=False, sheet_name='Results')
                output.seek(0)

                st.download_button(
                    label="⬇️ Download as Excel (.xlsx)",
                    data=output,
                    file_name=f"{input_basename}_output.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    type="primary",
                    use_container_width=True
                )

            with col2:
                # CSV download
                csv = results_df.to_csv(index=False)
                st.download_button(
                    label="⬇️ Download as CSV (.csv)",
                    data=csv,
                    file_name=f"{input_basename}_output.csv",
                    mime="text/csv",
                    type="primary",
                    use_container_width=True
                )

            st.markdown("---")

            # Summary statistics
            st.subheader("Summary Statistics")

            col1, col2, col3, col4 = st.columns(4)

            with col1:
                total_patents = len(results_df)
                st.metric("Total Patents", total_patents)

            with col2:
                if 'relevance' in results_df.columns:
                    relevant_count = len(results_df[results_df['relevance'] == 'RELEVANT'])
                    st.metric("Relevant Patents", relevant_count, f"{(relevant_count/total_patents*100):.1f}%")
                else:
                    st.metric("Relevant Patents", "N/A")

            with col3:
                if 'relevance_percentage' in results_df.columns:
                    avg_relevance = results_df['relevance_percentage'].mean()
                    st.metric("Avg Relevance Score", f"{avg_relevance:.1f}%")
                else:
                    st.metric("Avg Relevance Score", "N/A")

            with col4:
                if 'confidence' in results_df.columns:
                    high_confidence = len(results_df[results_df['confidence'] == 'HIGH'])
                    st.metric("High Confidence", high_confidence)
                else:
                    st.metric("High Confidence", "N/A")

            # Charts
            col1, col2 = st.columns(2)

            with col1:
                st.subheader("Relevance Distribution")
                if 'relevance' in results_df.columns:
                    relevance_counts = results_df['relevance'].value_counts()
                    st.bar_chart(relevance_counts)
                else:
                    st.info("No 'relevance' column found in results")

            with col2:
                st.subheader("Confidence Distribution")
                if 'confidence' in results_df.columns:
                    confidence_counts = results_df['confidence'].value_counts()
                    st.bar_chart(confidence_counts)
                else:
                    st.info("No 'confidence' column found in results")

            # Relevant patents details
            st.subheader("Relevant Patents")

            # Check if required columns exist
            if 'relevance' in results_df.columns:
                # Sort by relevance_percentage if it exists, otherwise don't sort
                if 'relevance_percentage' in results_df.columns:
                    relevant_patents = results_df[results_df['relevance'] == 'RELEVANT'].sort_values('relevance_percentage', ascending=False)
                else:
                    relevant_patents = results_df[results_df['relevance'] == 'RELEVANT']
            else:
                relevant_patents = pd.DataFrame()  # Empty dataframe if no relevance column

            if len(relevant_patents) > 0:
                for idx, patent in relevant_patents.iterrows():
                    with st.expander(f"📄 {patent.get(column_mapping['publication_number'], 'N/A')} - {patent.get('relevance_percentage', 0):.0f}%"):
                        col1, col2 = st.columns([2, 1])

                        with col1:
                            st.write(f"**Title:** {patent.get(column_mapping['title'], 'N/A')}")
                            st.write(f"**Reasoning:** {patent.get('reasoning', 'N/A')}")
                            if patent.get('key_features_found'):
                                st.write(f"**Key Features:** {patent.get('key_features_found')}")
                            if patent.get('protocols_mentioned'):
                                st.write(f"**Protocols:** {patent.get('protocols_mentioned')}")

                        with col2:
                            st.metric("Relevance", f"{patent.get('relevance_percentage', 0):.0f}%")
                            st.write(f"**Confidence:** {patent.get('confidence', 'N/A')}")
                            st.write(f"**Source:** {patent.get('relevance_source', 'N/A')}")
            else:
                st.info("No relevant patents found.")

            # Full results table
            st.subheader("All Results")
            st.dataframe(results_df, use_container_width=True)

    except Exception as e:
        st.error(f"❌ Error reading file: {str(e)}")
        st.stop()

else:
    st.info("👆 Please upload a file to get started")

    # Show instructions
    with st.expander("ℹ️ How to use this tool"):
        st.markdown("""
        ### Instructions:

        1. **Configure Azure OpenAI Settings** (in sidebar):
           - Enter your API Key
           - Enter your Endpoint URL
           - Verify API Version and Model Name

        2. **Upload Your Patent Data**:
           - Supported formats: CSV, TSV, Excel (.xlsx, .xls)
           - File should contain patent information columns
           - **Enable "Consolidate multi-row patent records"** if your CSV has patent descriptions split across multiple rows (common with patent database exports)

        3. **Map Columns**:
           - Select which columns in your file correspond to:
             - Publication Number
             - Title
             - Abstract
             - Claims (optional)
             - Description (optional)

        4. **Configure Prompt** (optional):
           - Use the default QKD analysis prompt
           - Or customize it for your specific needs
           - Use placeholders like `{publication_number}`, `{title}`, etc.

        5. **Start Analysis**:
           - Click "Start Analysis" button
           - Monitor progress as patents are analyzed
           - View results and statistics

        6. **Download Results**:
           - Export results as Excel or CSV
           - Results include relevance scores, confidence levels, and reasoning
        """)

# Footer
st.markdown("---")
# st.markdown("Made with Streamlit | QKD Patent Analyzer v1.0")
